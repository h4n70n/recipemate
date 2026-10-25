"""Rate-limit tests for ``POST /v1/recipes/extract``.

Complements :mod:`tests.recipes.test_extract` by exercising the per-user
fixed-window limiter wired into the endpoint (FEAT-002 of OR-5):

* requests under the configured limit return 201,
* the request that exceeds the limit returns 429 WITH a ``Retry-After``
  header, and — because the limiter runs BEFORE the recipe is created and the
  job enqueued — that 429 creates NO recipe row and sends NO SQS message,
* a Redis outage lets the request through (the limiter fails open).

SQS is faked with :mod:`moto` so a real queue exists and we can assert exactly
how many messages landed. Redis is replaced by an in-memory stub implementing
the limiter surface (``incr``/``expire``/``ttl``), monkeypatched onto
``app.services.redis_client.get_redis_client``. Auth is stubbed to a seeded
owner, mirroring :mod:`tests.recipes.test_extract`.
"""

from __future__ import annotations

import boto3
import pytest
import redis
from moto import mock_aws

from app import db
from app.auth import decorators
from app.models.recipe import Recipe
from app.models.user import User
from app.services import redis_client

TEST_REGION = "us-east-1"
TEST_QUEUE_NAME = "recipemate-extraction"


class _FakeRedis:
    """In-memory redis-py stand-in supporting the limiter surface."""

    def __init__(self) -> None:
        self.ttls: dict[str, int] = {}
        self.counts: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    def ttl(self, key: str) -> int:
        if key not in self.counts:
            return -2
        return self.ttls.get(key, -1)


class _ExplodingRedis:
    """Redis stub whose every operation raises, simulating an outage."""

    def incr(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")

    def expire(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")

    def ttl(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _make_user(email: str, cognito_sub: str) -> User:
    user = User(email=email, cognito_sub=cognito_sub)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def owner(app, monkeypatch):
    with app.app_context():
        user = _make_user("owner@example.com", "sub-owner")
        owner_id = user.id

    monkeypatch.setattr(
        decorators, "validate_token", lambda token: {"sub": "sub-owner"}
    )
    monkeypatch.setattr(
        decorators,
        "resolve_current_user",
        lambda claims: User.query.get(owner_id),
    )
    return {"owner_id": owner_id}


@pytest.fixture()
def sqs_backend(app):
    with mock_aws():
        client = boto3.client("sqs", region_name=TEST_REGION)
        queue_url = client.create_queue(QueueName=TEST_QUEUE_NAME)["QueueUrl"]
        app.config["SQS_EXTRACTION_QUEUE_URL"] = queue_url
        app.config["AWS_REGION"] = TEST_REGION
        app.config["AWS_ENDPOINT_URL"] = ""
        yield client


def _message_count(sqs_backend, app) -> int:
    return len(
        sqs_backend.receive_message(
            QueueUrl=app.config["SQS_EXTRACTION_QUEUE_URL"],
            MaxNumberOfMessages=10,
        ).get("Messages", [])
    )


def test_under_limit_requests_succeed(
    client, app, owner, sqs_backend, monkeypatch
):
    app.config["RATE_LIMIT_EXTRACT_MAX"] = 3
    app.config["RATE_LIMIT_EXTRACT_WINDOW_SECONDS"] = 60
    monkeypatch.setattr(
        redis_client, "get_redis_client", lambda fake=_FakeRedis(): fake
    )

    for i in range(3):
        resp = client.post(
            "/v1/recipes/extract",
            json={"s3_key": f"uploads/x/{i}.jpg"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 201


def test_over_limit_returns_429_no_recipe_no_message(
    client, app, owner, sqs_backend, monkeypatch
):
    app.config["RATE_LIMIT_EXTRACT_MAX"] = 2
    app.config["RATE_LIMIT_EXTRACT_WINDOW_SECONDS"] = 60
    monkeypatch.setattr(
        redis_client, "get_redis_client", lambda fake=_FakeRedis(): fake
    )

    for i in range(2):
        assert (
            client.post(
                "/v1/recipes/extract",
                json={"s3_key": f"uploads/x/{i}.jpg"},
                headers=_auth_headers(),
            ).status_code
            == 201
        )

    # Drain the two legitimate messages so the over-limit assertion is clean.
    assert _message_count(sqs_backend, app) == 2

    blocked = client.post(
        "/v1/recipes/extract",
        json={"s3_key": "uploads/x/overflow.jpg"},
        headers=_auth_headers(),
    )
    assert blocked.status_code == 429
    assert "error" in blocked.get_json()
    assert "Retry-After" in blocked.headers
    assert int(blocked.headers["Retry-After"]) >= 1

    # The throttled request created NO recipe row beyond the two allowed ones.
    with app.app_context():
        count = Recipe.query.filter(
            Recipe.user_id == owner["owner_id"]
        ).count()
        assert count == 2

    # And it sent NO additional SQS message.
    assert _message_count(sqs_backend, app) == 0


def test_redis_outage_allows_request(
    client, app, owner, sqs_backend, monkeypatch
):
    app.config["RATE_LIMIT_EXTRACT_MAX"] = 1
    app.config["RATE_LIMIT_EXTRACT_WINDOW_SECONDS"] = 60
    monkeypatch.setattr(
        redis_client, "get_redis_client", lambda: _ExplodingRedis()
    )

    # Well past the limit of 1, but the outage fails open -> all succeed.
    for i in range(3):
        resp = client.post(
            "/v1/recipes/extract",
            json={"s3_key": f"uploads/x/{i}.jpg"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 201
