"""Tests for ``GET /v1/recipes/{id}`` — full recipe detail.

Exercises the endpoint's contract from ``docs/api.md`` under
``GET /recipes/{id}``:

* fetching an owned recipe returns the full detail object with its nested
  ingredients/tools/instructions in the documented order plus the
  ``cook_summary`` ``{cook_count, avg_rating}``,
* an unknown id returns 404,
* a non-UUID path segment returns 404 (Flask's ``uuid`` converter never routes
  it to the view),
* a soft-deleted recipe returns 404,
* a recipe owned by another user returns 403 (the documented
  "authenticated but not the resource owner" case),
* an unauthenticated request is rejected with 401.

Auth is stubbed exactly like :mod:`tests.recipes.test_create_recipe`:
``validate_token`` is replaced with a trivial decoder and
``resolve_current_user`` is pointed at a seeded user, so the tests never touch
Cognito/JWKS. The DB-backed ``app``/``client`` fixtures come from
:mod:`tests.conftest`.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest

from app import db
from app.auth import decorators
from app.models.recipe import (
    Ingredient,
    Instruction,
    Recipe,
    Tag,
    Tool,
)
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


def _seed_populated_recipe(user_id: uuid.UUID) -> uuid.UUID:
    """Create a recipe owned by ``user_id`` with nested detail rows.

    Ingredients, tools, and instructions are inserted out of their natural
    order so the test proves the view returns them sorted by
    ``sort_order``/``step_number`` rather than insertion order. Returns the new
    recipe id.
    """
    recipe = Recipe(
        user_id=user_id,
        title="Pancakes",
        description="Fluffy stack",
        cook_count=3,
        avg_rating=Decimal("4.50"),
    )
    db.session.add(recipe)
    db.session.flush()

    # Insert deliberately out of order (2 before 1) to exercise ordering.
    db.session.add_all(
        [
            Ingredient(
                recipe_id=recipe.id,
                name="Milk",
                quantity=Decimal("250"),
                unit="ml",
                sort_order=2,
            ),
            Ingredient(
                recipe_id=recipe.id,
                name="Flour",
                quantity=Decimal("200"),
                unit="g",
                preparation="sifted",
                sort_order=1,
            ),
        ]
    )
    db.session.add_all(
        [
            Tool(recipe_id=recipe.id, name="Whisk", sort_order=2),
            Tool(recipe_id=recipe.id, name="Bowl", sort_order=1),
        ]
    )
    db.session.add_all(
        [
            Instruction(recipe_id=recipe.id, step_number=2, body="Cook"),
            Instruction(recipe_id=recipe.id, step_number=1, body="Mix"),
        ]
    )

    breakfast = Tag(name="breakfast")
    sweet = Tag(name="sweet")
    db.session.add_all([breakfast, sweet])
    db.session.flush()
    recipe.tags.append(breakfast)
    recipe.tags.append(sweet)

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
# Tests
# ---------------------------------------------------------------------------


def test_get_own_recipe_returns_full_detail(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_populated_recipe(as_owner["owner_id"])

    resp = client.get(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["id"] == str(recipe_id)
    assert body["title"] == "Pancakes"
    assert body["description"] == "Fluffy stack"

    # Nested lists come back ordered.
    assert [i["name"] for i in body["ingredients"]] == ["Flour", "Milk"]
    assert [i["sort_order"] for i in body["ingredients"]] == [1, 2]
    assert body["ingredients"][0]["preparation"] == "sifted"

    assert [t["name"] for t in body["tools"]] == ["Bowl", "Whisk"]
    assert [t["sort_order"] for t in body["tools"]] == [1, 2]

    assert [s["body"] for s in body["instructions"]] == ["Mix", "Cook"]
    assert [s["step_number"] for s in body["instructions"]] == [1, 2]

    # Tags are plain name strings (order not asserted).
    assert set(body["tags"]) == {"breakfast", "sweet"}

    # Cook summary echoes the denormalised columns.
    assert body["cook_summary"] == {"cook_count": 3, "avg_rating": 4.5}


def test_unknown_id_returns_404(client, as_owner):
    missing = uuid.uuid4()
    resp = client.get(f"/v1/recipes/{missing}", headers=_auth_headers())
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Recipe not found"}


def test_non_uuid_id_returns_404(client, as_owner):
    resp = client.get("/v1/recipes/not-a-uuid", headers=_auth_headers())
    # Flask's <uuid:...> converter refuses to route a non-UUID segment.
    assert resp.status_code == 404


def test_soft_deleted_recipe_returns_404(client, app, as_owner):
    with app.app_context():
        recipe_id = _seed_populated_recipe(as_owner["owner_id"])
        recipe = Recipe.query.get(recipe_id)
        recipe.deleted_at = datetime.datetime.utcnow()
        db.session.commit()

    resp = client.get(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Recipe not found"}


def test_other_users_recipe_returns_403(client, app, as_owner):
    # A recipe owned by a different user.
    with app.app_context():
        other = _make_user("other@example.com", "sub-other")
        recipe_id = _seed_populated_recipe(other.id)

    resp = client.get(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 403
    assert "error" in resp.get_json()


def test_requires_auth(client):
    resp = client.get(f"/v1/recipes/{uuid.uuid4()}")
    assert resp.status_code == 401
