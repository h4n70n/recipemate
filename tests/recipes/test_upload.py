"""Tests for ``POST /v1/recipes/upload`` — presigned S3 upload URLs.

These exercise the endpoint's contract from ``docs/api.md`` under
``POST /recipes/upload``:

* a valid request returns 200 with an ``https`` ``upload_url`` and an
  ``s3_key`` of the form ``uploads/<uuid>/<basename>``,
* only the four documented ``content_type`` values are accepted (others 400),
* a missing/empty ``filename`` or an unknown field returns 400,
* an unauthenticated request is rejected with 401,
* a ``filename`` carrying path separators is sanitised to its basename (no
  traversal into the key),
* upload metadata is stored in Redis under ``upload:<s3_key>`` with the
  requesting user's id and a TTL,
* when Redis is unavailable the endpoint still returns 200 (best-effort
  metadata storage).

S3 is faked with :mod:`moto` so no real AWS is contacted and no credentials are
needed. Redis is replaced by an in-memory stub that monkeypatches
:func:`app.services.redis_client.get_redis_client`, so the suite never requires
a running Redis. Auth is stubbed exactly like
:mod:`tests.recipes.test_create_recipe`.
"""

from __future__ import annotations

import json
import re
import uuid

import boto3
import pytest
from moto import mock_aws

from app import db
from app.auth import decorators
from app.models.user import User
from app.services import redis_client

TEST_REGION = "us-east-1"
TEST_BUCKET = "recipemate-images"

# uploads/<uuid4>/<basename>
_KEY_RE = re.compile(
    r"^uploads/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}/(?P<basename>.+)$"
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal in-memory stand-in for a redis-py client.

    Backs ``set(..., ex=...)`` with a dict so tests can assert what was stored
    and with what TTL, without a running Redis. Only the surface the metadata
    helper touches is implemented.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True


class _ExplodingRedis:
    """Redis stub whose every operation raises, simulating an outage."""

    def set(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        import redis

        raise redis.ConnectionError("redis is down")


def _auth_headers() -> dict[str, str]:
    """A bearer header; the token value is irrelevant under the stub."""
    return {"Authorization": "Bearer test-token"}


def _make_user(email: str, cognito_sub: str) -> User:
    user = User(email=email, cognito_sub=cognito_sub)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def s3_backend():
    """Start moto's S3 mock and create the configured bucket for the test."""
    with mock_aws():
        client = boto3.client("s3", region_name=TEST_REGION)
        client.create_bucket(Bucket=TEST_BUCKET)
        yield client


@pytest.fixture()
def fake_redis(monkeypatch):
    """Point the metadata helper at an in-memory Redis stub; return it."""
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fake)
    return fake


@pytest.fixture()
def as_owner(app, monkeypatch):
    """Seed an owning user and wire the auth stubs; yield its id."""
    with app.app_context():
        owner = _make_user("owner@example.com", "sub-owner")
        owner_id = owner.id

    monkeypatch.setattr(
        decorators, "validate_token", lambda token: {"sub": "sub-owner"}
    )
    monkeypatch.setattr(
        decorators,
        "resolve_current_user",
        lambda claims: User.query.get(owner_id),
    )

    return {"owner_id": owner_id}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_upload_returns_url_and_key(client, as_owner, s3_backend, fake_redis):
    resp = client.post(
        "/v1/recipes/upload",
        json={"filename": "photo.jpg", "content_type": "image/jpeg"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["upload_url"].startswith("https://")
    match = _KEY_RE.match(body["s3_key"])
    assert match is not None, body["s3_key"]
    assert match.group("basename") == "photo.jpg"


@pytest.mark.parametrize(
    "content_type",
    ["image/jpeg", "image/png", "image/heic", "application/pdf"],
)
def test_all_supported_content_types_accepted(
    client, as_owner, s3_backend, fake_redis, content_type
):
    resp = client.post(
        "/v1/recipes/upload",
        json={"filename": "doc.bin", "content_type": content_type},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200


def test_metadata_stored_with_user_and_ttl(
    client, as_owner, s3_backend, fake_redis
):
    resp = client.post(
        "/v1/recipes/upload",
        json={"filename": "photo.jpg", "content_type": "image/jpeg"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    s3_key = resp.get_json()["s3_key"]

    redis_key = f"upload:{s3_key}"
    assert redis_key in fake_redis.store

    stored = json.loads(fake_redis.store[redis_key])
    assert stored["user_id"] == str(as_owner["owner_id"])
    assert stored["filename"] == "photo.jpg"
    assert stored["content_type"] == "image/jpeg"

    # TTL present and 15 minutes.
    assert fake_redis.ttls[redis_key] == 900


def test_filename_with_path_separators_is_sanitised(
    client, as_owner, s3_backend, fake_redis
):
    resp = client.post(
        "/v1/recipes/upload",
        json={
            "filename": "../../etc/passwd.jpg",
            "content_type": "image/jpeg",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    s3_key = resp.get_json()["s3_key"]

    match = _KEY_RE.match(s3_key)
    assert match is not None, s3_key
    # Only the basename survives — no traversal into the key.
    assert match.group("basename") == "passwd.jpg"
    assert ".." not in s3_key


# ---------------------------------------------------------------------------
# Validation (400)
# ---------------------------------------------------------------------------


def test_unsupported_content_type_returns_400(
    client, as_owner, s3_backend, fake_redis
):
    resp = client.post(
        "/v1/recipes/upload",
        json={"filename": "clip.mp4", "content_type": "video/mp4"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_missing_filename_returns_400(client, as_owner, s3_backend, fake_redis):
    resp = client.post(
        "/v1/recipes/upload",
        json={"content_type": "image/jpeg"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_empty_filename_returns_400(client, as_owner, s3_backend, fake_redis):
    resp = client.post(
        "/v1/recipes/upload",
        json={"filename": "   ", "content_type": "image/jpeg"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_unknown_field_returns_400(client, as_owner, s3_backend, fake_redis):
    resp = client.post(
        "/v1/recipes/upload",
        json={
            "filename": "photo.jpg",
            "content_type": "image/jpeg",
            "size": 1234,
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# Auth (401)
# ---------------------------------------------------------------------------


def test_requires_auth(client):
    resp = client.post(
        "/v1/recipes/upload",
        json={"filename": "photo.jpg", "content_type": "image/jpeg"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Best-effort Redis
# ---------------------------------------------------------------------------


def test_redis_down_still_returns_200(
    client, as_owner, s3_backend, monkeypatch
):
    """A Redis outage must not block issuing the presigned URL."""
    monkeypatch.setattr(
        redis_client, "get_redis_client", lambda: _ExplodingRedis()
    )

    resp = client.post(
        "/v1/recipes/upload",
        json={"filename": "photo.jpg", "content_type": "image/jpeg"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["upload_url"].startswith("https://")
    assert _KEY_RE.match(body["s3_key"]) is not None
