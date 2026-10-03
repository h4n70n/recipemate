"""Ownership-enforcement tests for the recipe write surface (TASK-2.1).

These consolidate the "a user can only modify their own recipes" guarantees
for the three recipe write operations documented in ``docs/api.md``:

* ``POST /recipes`` — create,
* ``PATCH /recipes/{id}`` — partial update,
* ``DELETE /recipes/{id}`` — soft delete.

Two classes of guarantee are covered:

1. **No cross-user access.** A caller patching or deleting a recipe owned by a
   *different* user is rejected with 403 and the target recipe is left
   unchanged. (These mirror the per-endpoint cases in
   :mod:`tests.recipes.test_update_recipe` / :mod:`tests.recipes.test_delete_recipe`;
   they are repeated here so the ownership contract is provable from one file.)

2. **No mass-assignment of ``user_id``.** A client cannot choose or change the
   owner of a recipe. ``RecipeCreate`` / ``RecipeUpdate`` both declare
   ``extra="forbid"`` and neither declares a ``user_id`` field, so sending
   ``user_id`` in the body is rejected with 400 — the owner is always the
   authenticated caller, set server-side from ``current_user.id``.

Auth is stubbed exactly like the sibling recipe tests: ``validate_token`` is
replaced with a trivial decoder and ``resolve_current_user`` is pointed at a
seeded user, so the tests never touch Cognito/JWKS. The DB-backed
``app``/``client`` fixtures come from :mod:`tests.conftest`.
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
def two_users(app, monkeypatch):
    """Seed an owner and a second user; auth resolves to the owner.

    Yields the owner id and the "other" user id. ``resolve_current_user`` is
    wired to the owner, so any request made via ``client`` is authenticated as
    the owner. The ``other`` id is a real, distinct user that recipes can be
    assigned to (for the non-owner 403 cases) and that can be referenced as a
    bogus owner in the mass-assignment payloads.
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


# ---------------------------------------------------------------------------
# Cross-user access is rejected (403) and leaves the target untouched
# ---------------------------------------------------------------------------


def test_patch_other_users_recipe_returns_403_and_is_unchanged(
    client, app, two_users
):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["other_id"], title="Theirs")

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"title": "Hijacked"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 403
    assert "error" in resp.get_json()

    # The non-owner's recipe is untouched (title unchanged, still owned by them).
    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        assert recipe.title == "Theirs"
        assert str(recipe.user_id) == str(two_users["other_id"])


def test_delete_other_users_recipe_returns_403_and_is_not_deleted(
    client, app, two_users
):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["other_id"], title="Theirs")

    resp = client.delete(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 403
    assert "error" in resp.get_json()

    # The non-owner's recipe is not soft-deleted and still owned by them.
    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        assert recipe is not None
        assert recipe.deleted_at is None
        assert str(recipe.user_id) == str(two_users["other_id"])


# ---------------------------------------------------------------------------
# No mass-assignment of user_id on create
# ---------------------------------------------------------------------------


def test_post_rejects_user_id_in_body(client, app, two_users):
    """POST cannot set an arbitrary owner: ``user_id`` is an unknown field.

    ``RecipeCreate`` declares ``extra="forbid"`` and has no ``user_id`` field,
    so sending one is a 400 and no recipe is created for the other user.
    """
    resp = client.post(
        "/v1/recipes",
        json={"title": "x", "user_id": str(two_users["other_id"])},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    # Nothing was persisted — in particular nothing owned by the other user.
    with app.app_context():
        assert Recipe.query.count() == 0


def test_post_creates_recipe_owned_by_caller(client, app, two_users):
    """A valid create is always owned by the authenticated caller.

    This is the positive counterpart to the rejection above: even though the
    only way to express an owner would be ``user_id`` (which is forbidden), the
    created recipe's ``user_id`` is the caller's id, never anyone else's.
    """
    resp = client.post(
        "/v1/recipes", json={"title": "Mine"}, headers=_auth_headers()
    )
    assert resp.status_code == 201
    recipe_id = resp.get_json()["id"]

    with app.app_context():
        recipe = Recipe.query.get(uuid.UUID(recipe_id))
        assert str(recipe.user_id) == str(two_users["owner_id"])
        assert str(recipe.user_id) != str(two_users["other_id"])


# ---------------------------------------------------------------------------
# No mass-assignment of user_id on update
# ---------------------------------------------------------------------------


def test_patch_rejects_user_id_in_body(client, app, two_users):
    """PATCH cannot transfer ownership: ``user_id`` is an unknown field.

    ``RecipeUpdate`` declares ``extra="forbid"`` and has no ``user_id`` field,
    so an attempt to reassign the owner is a 400 and the recipe stays with the
    caller.
    """
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"], title="Mine")

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"user_id": str(two_users["other_id"])},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    # Ownership is unchanged — still the caller's.
    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        assert str(recipe.user_id) == str(two_users["owner_id"])


def test_patch_rejects_user_id_even_alongside_valid_fields(
    client, app, two_users
):
    """A smuggled ``user_id`` is rejected whole even with valid fields present.

    ``extra="forbid"`` fails the entire body, so the valid ``title`` change is
    *not* applied either — an all-or-nothing rejection, no partial ownership
    leak.
    """
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"], title="Mine")

    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json={"title": "Renamed", "user_id": str(two_users["other_id"])},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    with app.app_context():
        recipe = Recipe.query.get(recipe_id)
        assert recipe.title == "Mine"  # valid field not applied
        assert str(recipe.user_id) == str(two_users["owner_id"])
