"""End-to-end integration tests for the cook-log surface (TASK-4.1).

The sibling :mod:`tests.cooks.test_cook_logs` module exercises each cook-log
endpoint in isolation (list/create/update/delete, validation, ownership,
cross-recipe isolation, plus a generative recalc property test). This module
adds the *compositional* story those per-endpoint tests do not tell: that the
four endpoints, driven in sequence over the real HTTP surface, keep a parent
recipe's denormalised ``cook_count``/``avg_rating`` correct across the whole
cook-log lifecycle — and, crucially, that the recalculation is observable
through the **public read path** (``GET /recipes/{id}`` → ``cook_summary`` and
the top-level ``cook_count``/``avg_rating``), not merely through the ORM.

Two flows are covered:

* :func:`test_cook_log_lifecycle_recalculates_stats_through_recipe_detail`
  walks a recipe from zero cooks through create/create/create (one unrated),
  two PATCHes (change a rating, then clear one), and two DELETEs back toward
  zero, asserting the stats at every step **both** via the DB and via
  ``GET /recipes/{id}`` so the denormalised columns are confirmed through the
  API, not just the model.
* :func:`test_photo_and_ordering_across_lifecycle` drives the photo wiring and
  reverse-chronological ordering across create/patch/delete: derived https
  ``photo_url`` ending in the stored key, re-derivation after a key change,
  and list membership/ordering after a delete.

Auth is stubbed exactly like :mod:`tests.cooks.test_cook_logs`:
``validate_token`` is replaced with a trivial decoder and
``resolve_current_user`` is pointed at a seeded owner, so the tests never touch
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
# Helpers / fixtures (mirrors tests/cooks/test_cook_logs.py)
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    """A bearer header; the token value is irrelevant under the stub."""
    return {"Authorization": "Bearer test-token"}


def _make_user(email: str, cognito_sub: str) -> User:
    user = User(email=email, cognito_sub=cognito_sub)
    db.session.add(user)
    db.session.commit()
    return user


def _recipe_stats(recipe_id: uuid.UUID):
    """Return ``(cook_count, avg_rating)`` straight from the ORM row.

    ``avg_rating`` is coerced to ``float | None`` so DB-side ``Numeric`` values
    compare cleanly against the floats the API returns.
    """
    recipe = Recipe.query.get(recipe_id)
    avg = recipe.avg_rating
    return recipe.cook_count, (float(avg) if avg is not None else None)


@pytest.fixture()
def owner(app, monkeypatch):
    """Seed a single owner and wire auth to resolve to it.

    Mirrors the ``two_users`` fixture in :mod:`tests.cooks.test_cook_logs` but
    returns just the owner id, since the integration flows operate entirely on
    the authenticated owner's own recipe.
    """
    with app.app_context():
        owner_user = _make_user("owner@example.com", "sub-owner")
        owner_id = owner_user.id

    monkeypatch.setattr(
        decorators, "validate_token", lambda token: {"sub": "sub-owner"}
    )
    monkeypatch.setattr(
        decorators,
        "resolve_current_user",
        lambda claims: User.query.get(owner_id),
    )

    return owner_id


def _create_recipe(client, **body) -> str:
    """POST a manual recipe and return its id (fails loudly on non-201)."""
    body.setdefault("title", "Pancakes")
    resp = client.post("/v1/recipes", json=body, headers=_auth_headers())
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["id"]


def _get_detail(client, recipe_id: str) -> dict:
    """GET the recipe detail and return the decoded JSON body."""
    resp = client.get(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _list_cooks(client, recipe_id: str) -> list[dict]:
    """GET the recipe's cook logs and return the list of cook objects."""
    resp = client.get(
        f"/v1/recipes/{recipe_id}/cooks", headers=_auth_headers()
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["cooks"]


def _post_cook(client, recipe_id: str, **body) -> dict:
    """POST a cook log (expecting 201) and return the created cook object."""
    resp = client.post(
        f"/v1/recipes/{recipe_id}/cooks", json=body, headers=_auth_headers()
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def _patch_cook(client, recipe_id: str, cook_id: str, **body) -> dict:
    """PATCH a cook log (expecting 200) and return the updated cook object."""
    resp = client.patch(
        f"/v1/recipes/{recipe_id}/cooks/{cook_id}",
        json=body,
        headers=_auth_headers(),
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _delete_cook(client, recipe_id: str, cook_id: str) -> None:
    """DELETE a cook log and assert the documented 204/empty-body contract."""
    resp = client.delete(
        f"/v1/recipes/{recipe_id}/cooks/{cook_id}", headers=_auth_headers()
    )
    assert resp.status_code == 204, resp.get_json()
    assert resp.data == b""


_COOK_FIELDS = {"id", "cooked_at", "notes", "rating", "photo_url", "created_at"}


def _assert_stats(client, app, recipe_id, *, cook_count, avg_rating):
    """Assert the stats match ``cook_count``/``avg_rating`` via BOTH paths.

    The recalculation is confirmed two ways so the test proves the
    denormalised columns are both persisted (ORM) and surfaced through the
    public read path:

    * via the DB row (``Recipe.cook_count`` / ``Recipe.avg_rating``), and
    * via ``GET /recipes/{id}`` — the top-level ``cook_count``/``avg_rating``
      *and* the nested ``cook_summary`` ``{cook_count, avg_rating}``.
    """
    with app.app_context():
        db_count, db_avg = _recipe_stats(uuid.UUID(recipe_id))
    assert db_count == cook_count
    assert db_avg == avg_rating

    detail = _get_detail(client, recipe_id)
    assert detail["cook_count"] == cook_count
    assert detail["avg_rating"] == avg_rating
    assert detail["cook_summary"] == {
        "cook_count": cook_count,
        "avg_rating": avg_rating,
    }


# ---------------------------------------------------------------------------
# Full cook-log lifecycle, verified through GET /recipes/{id}
# ---------------------------------------------------------------------------


def test_cook_log_lifecycle_recalculates_stats_through_recipe_detail(
    client, app, owner
):
    """Drive the whole cook-log lifecycle and confirm stats via the read path.

    One cohesive flow across all four endpoints, asserting the parent recipe's
    denormalised ``cook_count``/``avg_rating`` evolve correctly and remain
    visible through ``GET /recipes/{id}`` at every step:

    0 cooks → +rating4 → +rating2 → +unrated → patch(2→5) → clear(first)
    → delete(only-rated) → delete(rest) → 0 cooks.

    Validates: Requirements 4.1 (cook_count / avg_rating recalculation,
    visible through GET /recipes/{id}).
    """
    recipe_id = _create_recipe(client, title="Lifecycle Loaf")

    # --- Step 0: a brand-new recipe has no cooks. ---------------------------
    _assert_stats(client, app, recipe_id, cook_count=0, avg_rating=None)
    assert _list_cooks(client, recipe_id) == []

    # --- Step 1: first cook, rated 4 → avg 4.0, count 1. --------------------
    first = _post_cook(client, recipe_id, cooked_at="2024-06-01", rating=4)
    assert set(first) == _COOK_FIELDS
    assert first["rating"] == 4
    _assert_stats(client, app, recipe_id, cook_count=1, avg_rating=4.0)
    # The cook is now visible in the listing.
    assert [c["id"] for c in _list_cooks(client, recipe_id)] == [first["id"]]

    # --- Step 2: second cook, rated 2 → mean(4,2)=3.0, count 2. -------------
    second = _post_cook(client, recipe_id, cooked_at="2024-06-02", rating=2)
    _assert_stats(client, app, recipe_id, cook_count=2, avg_rating=3.0)

    # --- Step 3: third cook, unrated → count 3, avg unchanged 3.0. ----------
    # An unrated log bumps the count but is excluded from the average.
    third = _post_cook(client, recipe_id, cooked_at="2024-06-03")
    assert third["rating"] is None
    _assert_stats(client, app, recipe_id, cook_count=3, avg_rating=3.0)

    # --- Step 4: PATCH the second log 2 → 5 → mean(4,5)=4.5, count 3. -------
    updated = _patch_cook(client, recipe_id, second["id"], rating=5)
    assert updated["rating"] == 5
    _assert_stats(client, app, recipe_id, cook_count=3, avg_rating=4.5)

    # --- Step 5: clear the first log's rating via null → only 5 remains. ----
    # avg becomes 5.0 (the second log), count still 3 (the log still exists).
    cleared = _patch_cook(client, recipe_id, first["id"], rating=None)
    assert cleared["rating"] is None
    _assert_stats(client, app, recipe_id, cook_count=3, avg_rating=5.0)

    # --- Step 6: DELETE the second (now the only rated) log. ----------------
    # Deleting the last rated log drops avg_rating back to NULL; count → 2.
    _delete_cook(client, recipe_id, second["id"])
    _assert_stats(client, app, recipe_id, cook_count=2, avg_rating=None)
    remaining_ids = {c["id"] for c in _list_cooks(client, recipe_id)}
    assert remaining_ids == {first["id"], third["id"]}

    # --- Step 7: DELETE the remaining logs → back to zero. ------------------
    _delete_cook(client, recipe_id, first["id"])
    _delete_cook(client, recipe_id, third["id"])
    _assert_stats(client, app, recipe_id, cook_count=0, avg_rating=None)
    assert _list_cooks(client, recipe_id) == []


# ---------------------------------------------------------------------------
# Photo wiring + reverse-chronological ordering across the flow
# ---------------------------------------------------------------------------


def test_photo_and_ordering_across_lifecycle(client, app, owner):
    """Photos derive https URLs and the listing stays reverse-chronological.

    Creates two cooks with ``photo_s3_key`` on different ``cooked_at`` dates,
    asserts the list comes back newest-first with a derived ``https``
    ``photo_url`` ending in the stored key, PATCHes one key and confirms the
    derived url re-resolves, then deletes one and confirms the surviving
    list/order.

    Validates: Requirements 4.1 (photo_url derivation + listing order).
    """
    recipe_id = _create_recipe(client, title="Photo Flow")

    older_key = "uploads/older/first.jpg"
    newer_key = "uploads/newer/second.jpg"

    # Insert the older-dated cook first, then the newer one, so insertion
    # order is the opposite of the expected (date-descending) listing order.
    older = _post_cook(
        client, recipe_id, cooked_at="2024-07-01", photo_s3_key=older_key
    )
    newer = _post_cook(
        client, recipe_id, cooked_at="2024-07-10", photo_s3_key=newer_key
    )

    # Each created cook exposes a derived https photo_url ending in its key.
    for cook, key in ((older, older_key), (newer, newer_key)):
        assert set(cook) == _COOK_FIELDS
        assert cook["photo_url"] is not None
        assert cook["photo_url"].startswith("https://")
        assert cook["photo_url"].endswith(key)

    # Listing is reverse-chronological: the 07-10 cook precedes the 07-01 cook,
    # and each carries its own derived url.
    listed = _list_cooks(client, recipe_id)
    assert [c["id"] for c in listed] == [newer["id"], older["id"]]
    assert listed[0]["photo_url"].endswith(newer_key)
    assert listed[1]["photo_url"].endswith(older_key)

    # --- PATCH the older cook's photo key → derived url re-resolves. --------
    replacement_key = "uploads/older/replacement.png"
    patched = _patch_cook(
        client, recipe_id, older["id"], photo_s3_key=replacement_key
    )
    assert patched["photo_url"].startswith("https://")
    assert patched["photo_url"].endswith(replacement_key)
    assert not patched["photo_url"].endswith(older_key)

    # The change is visible through the listing (ordering unchanged).
    listed = _list_cooks(client, recipe_id)
    assert [c["id"] for c in listed] == [newer["id"], older["id"]]
    assert listed[1]["photo_url"].endswith(replacement_key)

    # --- DELETE the newer cook → only the older (patched) cook remains. -----
    _delete_cook(client, recipe_id, newer["id"])
    listed = _list_cooks(client, recipe_id)
    assert [c["id"] for c in listed] == [older["id"]]
    assert listed[0]["photo_url"].endswith(replacement_key)
