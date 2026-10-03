"""Tests for ``POST /v1/recipes`` — manual-entry recipe creation.

These exercise the endpoint's contract from ``docs/api.md`` under
``POST /recipes``:

* ``title`` alone is enough to create a recipe (201) and the response is the
  full detail object with empty nested lists,
* every documented field round-trips (persisted + echoed back),
* validation failures (missing/empty title, bad origin) return 400 with
  ``{"error": ...}``,
* the created recipe is owned by the caller and shows up in ``GET /recipes``,
* an unauthenticated request is rejected with 401.

Auth is stubbed exactly like :mod:`tests.recipes.test_list_recipes`:
``validate_token`` is replaced with a trivial decoder and
``resolve_current_user`` is pointed at a seeded user, so the tests never touch
Cognito/JWKS. The DB-backed ``app``/``client`` fixtures come from
:mod:`tests.conftest`.
"""

from __future__ import annotations

import uuid

import pytest

from app import db
from app.auth import decorators
from app.models.recipe import Recipe
from app.models.user import User


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
# Tests
# ---------------------------------------------------------------------------


def test_minimal_create_returns_full_detail(client, as_owner):
    resp = client.post(
        "/v1/recipes", json={"title": "Toast"}, headers=_auth_headers()
    )
    assert resp.status_code == 201
    body = resp.get_json()

    assert body["title"] == "Toast"
    # Full detail shape with the nested collections present and empty.
    assert body["ingredients"] == []
    assert body["tools"] == []
    assert body["instructions"] == []
    assert body["tags"] == []
    assert body["cook_summary"] == {"cook_count": 0, "avg_rating": None}
    assert body["cook_count"] == 0
    assert body["extraction_status"] is None
    assert body["image_url"] is None
    assert body["thumbnail_url"] is None
    # Scalar optionals default to null.
    assert body["description"] is None
    assert body["origin"] is None
    # Server-populated fields.
    assert body["id"]
    assert body["created_at"]
    assert body["updated_at"]


def test_create_with_all_fields_persists_and_echoes(client, app, as_owner):
    payload = {
        "title": "Carbonara",
        "description": "Roman classic",
        "prep_time_min": 10,
        "cook_time_min": 15,
        "total_time_min": 25,
        "servings": 2,
        "origin": "cookbook",
        "source_url": "https://example.com/carbonara",
        "source_citation": "Nonna's book, p.42",
    }

    resp = client.post("/v1/recipes", json=payload, headers=_auth_headers())
    assert resp.status_code == 201
    body = resp.get_json()

    for key, value in payload.items():
        assert body[key] == value

    # Persisted with the documented values.
    with app.app_context():
        recipe = Recipe.query.get(uuid.UUID(body["id"]))
        assert recipe is not None
        assert recipe.title == "Carbonara"
        assert recipe.description == "Roman classic"
        assert recipe.prep_time_min == 10
        assert recipe.cook_time_min == 15
        assert recipe.total_time_min == 25
        assert recipe.servings == 2
        assert recipe.origin == "cookbook"
        assert recipe.source_url == "https://example.com/carbonara"
        assert recipe.source_citation == "Nonna's book, p.42"


def test_missing_title_returns_400(client, as_owner):
    resp = client.post("/v1/recipes", json={}, headers=_auth_headers())
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_invalid_origin_returns_400(client, as_owner):
    resp = client.post(
        "/v1/recipes",
        json={"title": "X", "origin": "tiktok"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_empty_or_whitespace_title_returns_400(client, as_owner):
    for bad_title in ["", "   "]:
        resp = client.post(
            "/v1/recipes",
            json={"title": bad_title},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400, bad_title
        assert "error" in resp.get_json()


def test_unknown_field_returns_400(client, as_owner):
    resp = client.post(
        "/v1/recipes",
        json={"title": "X", "calories": 500},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_negative_time_returns_400(client, as_owner):
    resp = client.post(
        "/v1/recipes",
        json={"title": "X", "prep_time_min": -5},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_created_recipe_is_owned_and_listed(client, app, as_owner):
    resp = client.post(
        "/v1/recipes", json={"title": "Mine"}, headers=_auth_headers()
    )
    assert resp.status_code == 201
    recipe_id = resp.get_json()["id"]

    # Owned by the authenticated caller.
    with app.app_context():
        recipe = Recipe.query.get(uuid.UUID(recipe_id))
        assert str(recipe.user_id) == str(as_owner["owner_id"])

    # Appears in the list endpoint.
    list_resp = client.get("/v1/recipes", headers=_auth_headers())
    body = list_resp.get_json()
    assert body["total"] == 1
    assert [r["id"] for r in body["recipes"]] == [recipe_id]


def test_requires_auth(client):
    resp = client.post("/v1/recipes", json={"title": "X"})
    assert resp.status_code == 401


def test_title_is_stripped(client, app, as_owner):
    resp = client.post(
        "/v1/recipes", json={"title": "  Padded  "}, headers=_auth_headers()
    )
    assert resp.status_code == 201
    assert resp.get_json()["title"] == "Padded"
