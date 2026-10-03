"""Tests for the async recipe-extraction API (TASK-2.3).

Covers the two endpoints documented in ``docs/api.md``:

* ``POST /recipes/extract`` — creates a ``pending`` recipe owned by the caller
  and enqueues an SQS extraction job, returning
  ``{"recipe_id", "extraction_status": "pending"}`` (201),
* ``GET /recipes/{id}/extraction-status`` — polls the recipe's extraction
  status (200), with the usual 404 (missing) / 403 (non-owner) / 401 (unauth)
  ownership rules.

SQS is faked with :mod:`moto` (``mock_aws``) so a real queue is created and we
can assert that a message with the right body actually landed — no running
broker needed. The queue URL moto returns is written into the app config so the
service's ``_require_queue_url`` resolves it. The enqueue-failure path is driven
by monkeypatching :func:`app.services.sqs.enqueue_extraction` to raise, which
lets us assert the 503 + no-orphan-recipe contract without depending on how the
failure is produced.

Auth is stubbed exactly like the sibling recipe tests: ``validate_token`` is
replaced with a trivial decoder and ``resolve_current_user`` is pointed at a
seeded user, so the tests never touch Cognito/JWKS. The DB-backed
``app``/``client`` fixtures come from :mod:`tests.conftest`.
"""

from __future__ import annotations

import json
import uuid

import boto3
import pytest
from moto import mock_aws

from app import db
from app.auth import decorators
from app.models.recipe import Recipe
from app.models.user import User
from app.services import sqs

TEST_REGION = "us-east-1"
TEST_QUEUE_NAME = "recipemate-extraction"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    """A bearer header; the token value is irrelevant under the stub."""
    return {"Authorization": "Bearer test-token"}


def _make_user(email: str, cognito_sub: str) -> User:
    user = User(email=email, cognito_sub=cognito_sub)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def two_users(app, monkeypatch):
    """Seed an owner and a second user; auth resolves to the owner.

    Yields both ids. ``resolve_current_user`` is wired to the owner, so any
    request made via ``client`` is authenticated as the owner; ``other_id`` is
    a real, distinct user used for the non-owner (403) case.
    """
    with app.app_context():
        owner = _make_user("owner@example.com", "sub-owner")
        other = _make_user("other@example.com", "sub-other")
        owner_id = owner.id
        other_id = other.id

    monkeypatch.setattr(
        decorators, "validate_token", lambda token: {"sub": "sub-owner"}
    )
    monkeypatch.setattr(
        decorators,
        "resolve_current_user",
        lambda claims: User.query.get(owner_id),
    )

    return {"owner_id": owner_id, "other_id": other_id}


@pytest.fixture()
def sqs_backend(app):
    """Start moto's SQS mock, create the queue, and wire its URL into config.

    Yields the boto3 SQS client (same mocked backend the app will use) so a
    test can read messages back off the queue and assert on them.
    """
    with mock_aws():
        client = boto3.client("sqs", region_name=TEST_REGION)
        queue_url = client.create_queue(QueueName=TEST_QUEUE_NAME)["QueueUrl"]
        app.config["SQS_EXTRACTION_QUEUE_URL"] = queue_url
        app.config["AWS_REGION"] = TEST_REGION
        app.config["AWS_ENDPOINT_URL"] = ""
        yield client


def _seed_recipe(user_id: uuid.UUID, **overrides) -> uuid.UUID:
    """Create a recipe owned by ``user_id`` and return its id."""
    fields = {
        "title": "Untitled recipe",
        "extraction_status": "pending",
        "cook_count": 0,
    }
    fields.update(overrides)
    recipe = Recipe(user_id=user_id, **fields)
    db.session.add(recipe)
    db.session.commit()
    return recipe.id


# ---------------------------------------------------------------------------
# POST /recipes/extract — happy path
# ---------------------------------------------------------------------------


def test_extract_creates_pending_recipe_and_enqueues(
    client, app, two_users, sqs_backend
):
    s3_key = "uploads/abc-123/photo.jpg"
    resp = client.post(
        "/v1/recipes/extract",
        json={"s3_key": s3_key, "origin": "instagram"},
        headers=_auth_headers(),
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["extraction_status"] == "pending"
    recipe_id = body["recipe_id"]
    # recipe_id is a well-formed uuid.
    uuid.UUID(recipe_id)

    # A pending recipe now exists, owned by the caller, with the key set.
    with app.app_context():
        recipe = Recipe.query.get(uuid.UUID(recipe_id))
        assert recipe is not None
        assert recipe.user_id == two_users["owner_id"]
        assert recipe.extraction_status == "pending"
        assert recipe.image_s3_key == s3_key
        assert recipe.origin == "instagram"

    # And an SQS message with the right body landed on the queue.
    messages = sqs_backend.receive_message(
        QueueUrl=app.config["SQS_EXTRACTION_QUEUE_URL"],
        MaxNumberOfMessages=10,
    ).get("Messages", [])
    assert len(messages) == 1
    sent = json.loads(messages[0]["Body"])
    assert sent["recipe_id"] == recipe_id
    assert sent["s3_key"] == s3_key
    assert sent["user_id"] == str(two_users["owner_id"])


def test_extract_without_origin_defaults_to_null(
    client, app, two_users, sqs_backend
):
    resp = client.post(
        "/v1/recipes/extract",
        json={"s3_key": "uploads/x/y.jpg"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 201
    recipe_id = resp.get_json()["recipe_id"]

    with app.app_context():
        recipe = Recipe.query.get(uuid.UUID(recipe_id))
        assert recipe.origin is None
        assert recipe.source_url is None


# ---------------------------------------------------------------------------
# POST /recipes/extract — validation (400)
# ---------------------------------------------------------------------------


def test_extract_missing_s3_key_returns_400(client, two_users, sqs_backend):
    resp = client.post(
        "/v1/recipes/extract",
        json={"origin": "instagram"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_extract_empty_s3_key_returns_400(client, two_users, sqs_backend):
    resp = client.post(
        "/v1/recipes/extract",
        json={"s3_key": "   "},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_extract_unknown_field_returns_400(client, two_users, sqs_backend):
    resp = client.post(
        "/v1/recipes/extract",
        json={"s3_key": "uploads/x/y.jpg", "bogus": 1},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_extract_invalid_origin_returns_400(client, two_users, sqs_backend):
    resp = client.post(
        "/v1/recipes/extract",
        json={"s3_key": "uploads/x/y.jpg", "origin": "tiktok"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_extract_requires_auth(client):
    resp = client.post(
        "/v1/recipes/extract",
        json={"s3_key": "uploads/x/y.jpg"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /recipes/extract — enqueue failure (503, no orphan recipe)
# ---------------------------------------------------------------------------


def test_extract_enqueue_failure_returns_503_and_leaves_no_recipe(
    client, app, two_users, monkeypatch
):
    def _boom(*args, **kwargs):
        raise RuntimeError("sqs is down")

    monkeypatch.setattr(sqs, "enqueue_extraction", _boom)

    resp = client.post(
        "/v1/recipes/extract",
        json={"s3_key": "uploads/x/y.jpg"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 503
    assert "error" in resp.get_json()

    # The recipe must have been rolled back — no orphaned pending row.
    with app.app_context():
        count = Recipe.query.filter(
            Recipe.user_id == two_users["owner_id"]
        ).count()
        assert count == 0


# ---------------------------------------------------------------------------
# GET /recipes/{id}/extraction-status
# ---------------------------------------------------------------------------


def test_extraction_status_reports_pending(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(
            two_users["owner_id"], extraction_status="pending"
        )

    resp = client.get(
        f"/v1/recipes/{recipe_id}/extraction-status",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"extraction_status": "pending"}


def test_extraction_status_reflects_completion(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(
            two_users["owner_id"], extraction_status="pending"
        )

    # Simulate the Lambda finishing the job.
    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        recipe.extraction_status = "complete"
        db.session.commit()

    resp = client.get(
        f"/v1/recipes/{recipe_id}/extraction-status",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"extraction_status": "complete"}


def test_extraction_status_non_owner_returns_403(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(
            two_users["other_id"], extraction_status="pending"
        )

    resp = client.get(
        f"/v1/recipes/{recipe_id}/extraction-status",
        headers=_auth_headers(),
    )
    assert resp.status_code == 403
    assert "error" in resp.get_json()


def test_extraction_status_missing_returns_404(client, two_users):
    missing = uuid.uuid4()
    resp = client.get(
        f"/v1/recipes/{missing}/extraction-status",
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_extraction_status_requires_auth(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(
            two_users["owner_id"], extraction_status="pending"
        )

    resp = client.get(f"/v1/recipes/{recipe_id}/extraction-status")
    assert resp.status_code == 401
