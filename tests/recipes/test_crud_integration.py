"""End-to-end integration tests for the recipe CRUD surface (TASK-2.1).

Where the sibling modules (``test_list_recipes``, ``test_create_recipe``,
``test_get_recipe``, ``test_update_recipe``, ``test_delete_recipe``) each prove
one endpoint's contract in isolation, this module proves the endpoints
*compose* correctly: a single caller drives a recipe through its whole
lifecycle over the real HTTP surface and the state observed by one endpoint is
exactly the state produced by the previous one.

Two flows are covered:

* :func:`test_full_crud_lifecycle` — list→create→list→get→patch→get→delete→
  (get 404 / list empty), asserting the documented response shapes
  (``docs/api.md``) at each hop and that writes persist across separate
  requests.
* :func:`test_multi_recipe_pagination_and_isolation` — create three recipes,
  page through them newest-first with ``limit=2``, and confirm a second user's
  recipe never leaks into the caller's list and returns 403 on a direct GET.

Auth is stubbed exactly like the sibling recipe tests: ``validate_token`` is
replaced with a trivial decoder and ``resolve_current_user`` is pointed at a
seeded user, so the tests never touch Cognito/JWKS. The DB-backed
``app``/``client`` fixtures come from :mod:`tests.conftest`.
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
    """Create a recipe owned directly by ``user_id`` and return its id.

    Used only to plant a *second user's* recipe straight in the DB for the
    isolation flow — the caller's own recipes always go through the HTTP API.
    """
    fields = {"title": "Theirs", "origin": "manual", "cook_count": 0}
    fields.update(overrides)
    recipe = Recipe(user_id=user_id, **fields)
    db.session.add(recipe)
    db.session.commit()
    return recipe.id


@pytest.fixture()
def as_owner(app, monkeypatch):
    """Seed an owning user and a second user; auth resolves to the owner.

    Yields both ids. Every request made through ``client`` is authenticated as
    the owner (``resolve_current_user`` is pinned to it); ``other_id`` is a
    real, distinct user that recipes can be assigned to for the isolation
    checks.
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
# Full CRUD lifecycle
# ---------------------------------------------------------------------------


def test_full_crud_lifecycle(client, app, as_owner):
    """Drive one recipe through its entire lifecycle over HTTP.

    Each step asserts the state the *previous* step should have produced, so a
    pass proves the endpoints agree on the stored state (not just that each
    one behaves on its own) and that writes persist across separate requests.
    """
    # 1. Start empty: no recipes for this caller.
    resp = client.get("/v1/recipes", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.get_json() == {"recipes": [], "total": 0}

    # 2. Create a recipe from manual fields -> 201 full detail.
    create_payload = {
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
    resp = client.post(
        "/v1/recipes", json=create_payload, headers=_auth_headers()
    )
    assert resp.status_code == 201
    created = resp.get_json()
    recipe_id = created["id"]
    assert recipe_id

    # Response is the full detail object with the documented nested shape.
    for key, value in create_payload.items():
        assert created[key] == value
    assert created["ingredients"] == []
    assert created["tools"] == []
    assert created["instructions"] == []
    assert created["tags"] == []
    assert created["cook_summary"] == {"cook_count": 0, "avg_rating": None}
    assert created["created_at"]
    assert created["updated_at"]
    created_updated_at = created["updated_at"]

    # 3. List now has exactly this one card and the card matches.
    resp = client.get("/v1/recipes", headers=_auth_headers())
    assert resp.status_code == 200
    listing = resp.get_json()
    assert listing["total"] == 1
    assert len(listing["recipes"]) == 1
    card = listing["recipes"][0]
    # List items are the documented compact *card* shape, not full detail.
    assert set(card.keys()) == {
        "id",
        "title",
        "thumbnail_url",
        "total_time_min",
        "cook_count",
        "avg_rating",
        "tags",
        "created_at",
    }
    assert card["id"] == recipe_id
    assert card["title"] == "Carbonara"
    assert card["total_time_min"] == 25

    # 4. GET /{id} returns full detail matching what was created.
    resp = client.get(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 200
    fetched = resp.get_json()
    assert fetched == created

    # Make the stored ``updated_at`` observably old so the PATCH bump is
    # unambiguous even when both requests land in the same clock tick.
    with app.app_context():
        recipe = Recipe.query.get(uuid.UUID(recipe_id))
        recipe.updated_at = datetime.datetime(2000, 1, 1, 0, 0, 0)
        db.session.commit()

    # 5. PATCH: change a couple of fields and clear one with an explicit null.
    patch_payload = {
        "title": "Carbonara (revised)",
        "servings": 4,
        "source_citation": None,
    }
    resp = client.patch(
        f"/v1/recipes/{recipe_id}",
        json=patch_payload,
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    patched = resp.get_json()
    assert patched["title"] == "Carbonara (revised)"
    assert patched["servings"] == 4
    assert patched["source_citation"] is None
    # Fields not mentioned keep their created values.
    assert patched["description"] == "Roman classic"
    assert patched["origin"] == "cookbook"
    assert patched["total_time_min"] == 25
    # created_at is stable; updated_at advanced past the created snapshot.
    assert patched["created_at"] == created["created_at"]
    assert patched["updated_at"] > created_updated_at

    # 6. GET /{id} reflects the patched state (persistence across requests).
    resp = client.get(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.get_json() == patched

    # 7. DELETE -> 204 with an empty body.
    resp = client.delete(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 204
    assert resp.get_data() == b""

    # 8. The recipe is gone from reads: GET /{id} 404, list empty again.
    resp = client.get(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Recipe not found"}

    resp = client.get("/v1/recipes", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.get_json() == {"recipes": [], "total": 0}


# ---------------------------------------------------------------------------
# Multi-recipe pagination + cross-user isolation
# ---------------------------------------------------------------------------


def test_multi_recipe_pagination_and_isolation(client, app, as_owner):
    """Page through several recipes and prove another user's never leaks in."""
    # Create three recipes over HTTP, oldest first. Flask's test client runs
    # each request in its own clock tick, so insertion order == age order and
    # the newest is the last one created.
    ids_in_creation_order = []
    for title in ("First", "Second", "Third"):
        resp = client.post(
            "/v1/recipes", json={"title": title}, headers=_auth_headers()
        )
        assert resp.status_code == 201
        ids_in_creation_order.append(resp.get_json()["id"])

    # Newest-first expected ordering is the reverse of creation order.
    newest_first = list(reversed(ids_in_creation_order))

    # Plant a second user's recipe directly; it must stay invisible.
    with app.app_context():
        other_recipe_id = _seed_recipe(as_owner["other_id"], title="Theirs")

    # Page 1: limit=2 -> the two newest, total counts all three.
    resp = client.get("/v1/recipes?limit=2&offset=0", headers=_auth_headers())
    assert resp.status_code == 200
    page1 = resp.get_json()
    assert page1["total"] == 3
    assert [r["id"] for r in page1["recipes"]] == newest_first[:2]
    assert [r["title"] for r in page1["recipes"]] == ["Third", "Second"]

    # Page 2: the remaining (oldest) recipe.
    resp = client.get("/v1/recipes?limit=2&offset=2", headers=_auth_headers())
    assert resp.status_code == 200
    page2 = resp.get_json()
    assert page2["total"] == 3
    assert [r["id"] for r in page2["recipes"]] == newest_first[2:]
    assert [r["title"] for r in page2["recipes"]] == ["First"]

    # The two pages together are the full newest-first set, with no overlap and
    # no sign of the other user's recipe.
    paged_ids = [r["id"] for r in page1["recipes"]] + [
        r["id"] for r in page2["recipes"]
    ]
    assert paged_ids == newest_first
    assert str(other_recipe_id) not in paged_ids

    # An unpaged list is also exactly the caller's three, newest-first.
    resp = client.get("/v1/recipes", headers=_auth_headers())
    assert resp.status_code == 200
    full = resp.get_json()
    assert full["total"] == 3
    assert [r["id"] for r in full["recipes"]] == newest_first

    # Direct access to the other user's recipe is forbidden, not merely hidden.
    resp = client.get(
        f"/v1/recipes/{other_recipe_id}", headers=_auth_headers()
    )
    assert resp.status_code == 403
    assert "error" in resp.get_json()
