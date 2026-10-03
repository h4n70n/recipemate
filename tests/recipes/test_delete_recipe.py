"""Tests for ``DELETE /v1/recipes/{id}`` — soft delete.

Exercises the endpoint's contract from ``docs/api.md`` under
``DELETE /recipes/{id}``:

* deleting an owned recipe returns 204 with an empty body and stamps the row's
  ``deleted_at`` (verified by querying the DB directly — the row is *not*
  physically removed),
* after a delete the recipe is gone from reads: ``GET /recipes/{id}`` returns
  404 and the recipe is absent from the ``GET /recipes`` list (``total``
  decremented),
* deleting an already soft-deleted recipe returns 404 (it is treated as
  missing — a near-idempotent no-op rather than a second 204),
* a recipe owned by another user returns 403 and is NOT deleted,
* an unknown id returns 404,
* a non-UUID path segment returns 404 (Flask's ``uuid`` converter never routes
  it to the view),
* an unauthenticated request is rejected with 401.

Auth is stubbed exactly like :mod:`tests.recipes.test_update_recipe`:
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
    """Create a recipe owned by ``user_id`` and return its id."""
    fields = {
        "title": "Original",
        "description": "Original description",
        "origin": "manual",
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
# Successful soft delete (204)
# ---------------------------------------------------------------------------


def test_delete_own_recipe_returns_204_empty_body_and_sets_deleted_at(
    client, app, as_owner
):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    before = datetime.datetime.utcnow()
    resp = client.delete(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    after = datetime.datetime.utcnow()

    assert resp.status_code == 204
    # No body is returned on 204.
    assert resp.get_data() == b""

    # The row still exists but is now soft-deleted with a sensible timestamp.
    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        assert recipe is not None
        assert recipe.deleted_at is not None
        assert before <= recipe.deleted_at <= after


def test_deleted_recipe_disappears_from_reads(client, app, as_owner):
    with app.app_context():
        # Two recipes so we can watch the list total drop by exactly one.
        kept_id = _seed_recipe(as_owner["owner_id"], title="Kept")
        doomed_id = _seed_recipe(as_owner["owner_id"], title="Doomed")

    # Baseline: both show up in the list.
    resp = client.get("/v1/recipes", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 2
    assert {r["id"] for r in body["recipes"]} == {str(kept_id), str(doomed_id)}

    # Delete one.
    resp = client.delete(f"/v1/recipes/{doomed_id}", headers=_auth_headers())
    assert resp.status_code == 204

    # GET /recipes/{id} on the deleted one is now a 404.
    resp = client.get(f"/v1/recipes/{doomed_id}", headers=_auth_headers())
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Recipe not found"}

    # The deleted recipe is absent from the list and total dropped by one.
    resp = client.get("/v1/recipes", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 1
    assert {r["id"] for r in body["recipes"]} == {str(kept_id)}


# ---------------------------------------------------------------------------
# Repeat / existence / ownership (404 / 403)
# ---------------------------------------------------------------------------


def test_delete_already_deleted_returns_404(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_recipe(as_owner["owner_id"])

    first = client.delete(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert first.status_code == 204

    # Second delete sees an already soft-deleted recipe → treated as missing.
    second = client.delete(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert second.status_code == 404
    assert second.get_json() == {"error": "Recipe not found"}


def test_other_users_recipe_returns_403_and_is_not_deleted(
    client, app, as_owner
):
    with app.app_context():
        other = _make_user("other@example.com", "sub-other")
        recipe_id = _seed_recipe(other.id)

    resp = client.delete(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 403
    assert "error" in resp.get_json()

    # The other user's recipe is untouched (not soft-deleted).
    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        assert recipe is not None
        assert recipe.deleted_at is None


def test_unknown_id_returns_404(client, as_owner):
    missing = uuid.uuid4()
    resp = client.delete(f"/v1/recipes/{missing}", headers=_auth_headers())
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Recipe not found"}


def test_non_uuid_id_returns_404(client, as_owner):
    resp = client.delete("/v1/recipes/not-a-uuid", headers=_auth_headers())
    # Flask's <uuid:...> converter refuses to route a non-UUID segment.
    assert resp.status_code == 404


def test_requires_auth(client):
    resp = client.delete(f"/v1/recipes/{uuid.uuid4()}")
    assert resp.status_code == 401
