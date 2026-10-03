"""Tests for ``PATCH /v1/recipes/{id}`` — partial recipe update.

Exercises the endpoint's contract from ``docs/api.md`` under
``PATCH /recipes/{id}``:

* patching a single field (e.g. ``title``) returns the updated full detail
  (200) and changes only that field,
* patching several fields at once applies all of them,
* fields the client omits are left untouched,
* explicitly sending ``null`` clears a nullable field (e.g. ``description``),
* an empty/whitespace-only ``title`` is rejected with 400,
* an invalid ``origin`` is rejected with 400,
* an unknown field is rejected with 400,
* a recipe owned by another user returns 403,
* a missing or soft-deleted recipe returns 404,
* an unauthenticated request is rejected with 401,
* ``updated_at`` advances after a successful patch.

Auth is stubbed exactly like :mod:`tests.recipes.test_get_recipe`:
``validate_token`` is replaced with a trivial decoder and
``resolve_current_user`` is pointed at a seeded user, so the tests never touch
Cognito/JWKS. The DB-backed ``app``/``client`` fixtures come from
:mod:`tests.conftest`.
"""

from __future__ import annotations

import datetime
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


def _seed_recipe(user_id: uuid.UUID, **overrides) -> uuid.UUID:
    """Create a recipe owned by ``user_id`` and return its id.

    Defaults give a fully-populated scalar set so tests can prove that
    omitted fields stay untouched and that explicit nulls clear them.
    """
    fields = {
        "title": "Original",
        "description": "Original description",
        "prep_time_min": 5,
        "cook_time_min": 10,
        "total_time_min": 15,
        "servings": 2,
        "origin": "manual",
        "source_url": "https://example.com/original",
        "source_citation": "Original citation",
        "cook_count": 0,
    }
    fields.update(overrides)
    recipe = Recipe(user_id=user_id, **fields)
    db.session.add(recipe)
    db.session.commit()
    return recipe.id


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
# Successful partial updates
# ---------------------------------------------------------------------------


def test_patch_single_field_changes_only_that_field(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"title": "Renamed"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()

    # Only the title changed; everything else keeps its seeded value.
    assert body["title"] == "Renamed"
    assert body["description"] == "Original description"
    assert body["prep_time_min"] == 5
    assert body["cook_time_min"] == 10
    assert body["total_time_min"] == 15
    assert body["servings"] == 2
    assert body["origin"] == "manual"
    assert body["source_url"] == "https://example.com/original"
    assert body["source_citation"] == "Original citation"

    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        assert recipe.title == "Renamed"
        assert recipe.description == "Original description"


def test_patch_multiple_fields(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={
            "title": "Multi",
            "servings": 8,
            "origin": "web",
            "total_time_min": 45,
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["title"] == "Multi"
    assert body["servings"] == 8
    assert body["origin"] == "web"
    assert body["total_time_min"] == 45
    # Untouched fields keep their seeded values.
    assert body["description"] == "Original description"
    assert body["prep_time_min"] == 5


def test_omitted_fields_unchanged(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    # Empty body: nothing is sent, so nothing changes.
    resp = client.patch(
        f"/v1/recipes/{recipe_id}", json={}, headers=_auth_headers()
    )
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["title"] == "Original"
    assert body["description"] == "Original description"
    assert body["prep_time_min"] == 5
    assert body["cook_time_min"] == 10
    assert body["total_time_min"] == 15
    assert body["servings"] == 2
    assert body["origin"] == "manual"
    assert body["source_url"] == "https://example.com/original"
    assert body["source_citation"] == "Original citation"


def test_explicit_null_clears_nullable_field(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"description": None},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()

    # description cleared; sibling fields untouched.
    assert body["description"] is None
    assert body["title"] == "Original"
    assert body["servings"] == 2

    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        assert recipe.description is None


def test_explicit_null_clears_multiple_nullable_fields(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={
            "servings": None,
            "origin": None,
            "source_url": None,
            "total_time_min": None,
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["servings"] is None
    assert body["origin"] is None
    assert body["source_url"] is None
    assert body["total_time_min"] is None
    # Fields not mentioned keep their values.
    assert body["title"] == "Original"
    assert body["description"] == "Original description"


def test_updated_at_advances(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])
        # Force an "old" updated_at well in the past so the bump is observable.
        recipe = Recipe.query.get(recipe_id)
        old = datetime.datetime(2000, 1, 1, 0, 0, 0)
        recipe.updated_at = old
        db.session.commit()

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"title": "Touched"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200

    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        assert recipe.updated_at > datetime.datetime(2000, 1, 1, 0, 0, 0)


# ---------------------------------------------------------------------------
# Validation failures (400)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_title", ["", "   "])
def test_empty_or_whitespace_title_returns_400(client, app, as_owner, bad_title):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"title": bad_title},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    # The failed patch left the stored title unchanged.
    with app.app_context():
        assert Recipe.query.get(recipe_id).title == "Original"


def test_null_title_returns_400(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"title": None},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_invalid_origin_returns_400(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"origin": "tiktok"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_negative_time_returns_400(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"prep_time_min": -1},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_unknown_field_returns_400(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"calories": 500},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# Ownership / existence (403 / 404)
# ---------------------------------------------------------------------------


def test_other_users_recipe_returns_403(client, app, as_owner):
    with app.app_context():
        other = _make_user("other@example.com", "sub-other")
        recipe_id = _seed_recipe(other.id)

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"title": "Hijack"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 403
    assert "error" in resp.get_json()

    # The other user's recipe is untouched.
    with app.app_context():
        assert Recipe.query.get(recipe_id).title == "Original"


def test_unknown_id_returns_404(client, as_owner):
    missing = uuid.uuid4()
    resp = client.patch(
        f"/v1/recipes/{missing}",
        json={"title": "X"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Recipe not found"}


def test_soft_deleted_recipe_returns_404(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])
        recipe = Recipe.query.get(recipe_id)
        recipe.deleted_at = datetime.datetime.utcnow()
        db.session.commit()

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"title": "X"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Recipe not found"}


def test_non_uuid_id_returns_404(client, as_owner):
    resp = client.patch(
        "/v1/recipes/not-a-uuid",
        json={"title": "X"},
        headers=_auth_headers(),
    )
    # Flask's <uuid:...> converter refuses to route a non-UUID segment.
    assert resp.status_code == 404


def test_requires_auth(client):
    resp = client.patch(f"/v1/recipes/{uuid.uuid4()}", json={"title": "X"})
    assert resp.status_code == 401
