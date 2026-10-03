"""Tests for the cook log endpoints (TASK-4.1).

Covers the four recipe-scoped cook log endpoints documented in ``docs/api.md``
under *Cook Logs* (all under ``/v1/recipes/{id}/cooks[...]``):

* ``GET    /recipes/{id}/cooks``           — reverse-chronological listing,
* ``POST   /recipes/{id}/cooks``           — create (date required),
* ``PATCH  /recipes/{id}/cooks/{cook_id}`` — update notes/rating/photo,
* ``DELETE /recipes/{id}/cooks/{cook_id}`` — delete,

plus the critical shared concern: ``Recipe.cook_count`` and
``Recipe.avg_rating`` are recomputed after every create/update/delete.
``avg_rating`` is the mean of **rated** logs only (unrated logs are excluded,
not counted as 0) and is NULL when nothing is rated.

Both example-based (unit) assertions and a generative property test are
included: the generative test drives many randomised multisets of ratings
(seeded for reproducibility) through the real endpoints and asserts that the
recalculated ``avg_rating`` always equals the mean of the non-null ratings
(rounded to 2dp) and ``cook_count`` always equals the total number of logs. It
uses the stdlib rather than a property-testing library because the project does
not depend on one.

Auth is stubbed exactly like the sibling recipe/tag tests: ``validate_token``
is replaced with a trivial decoder and ``resolve_current_user`` is pointed at a
seeded owner, so the tests never touch Cognito/JWKS. The DB-backed
``app``/``client`` fixtures come from :mod:`tests.conftest`.
"""

from __future__ import annotations

import datetime
import itertools
import random
import uuid
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app import db
from app.auth import decorators
from app.models.cook_log import CookLog
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
    fields = {"title": "Pancakes", "cook_count": 0}
    fields.update(overrides)
    recipe = Recipe(user_id=user_id, **fields)
    db.session.add(recipe)
    db.session.commit()
    return recipe.id


def _recipe_stats(recipe_id: uuid.UUID):
    """Return ``(cook_count, avg_rating)`` for a recipe, avg as float|None."""
    recipe = Recipe.query.get(recipe_id)
    avg = recipe.avg_rating
    return recipe.cook_count, (float(avg) if avg is not None else None)


@pytest.fixture()
def two_users(app, monkeypatch):
    """Seed an owner and a second user; auth resolves to the owner.

    Mirrors the tag test's fixture: ``resolve_current_user`` is wired to the
    owner so every request via ``client`` is authenticated as the owner, while
    ``other`` is a real, distinct row recipes/cook logs can be assigned to for
    the cross-user cases.
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
# GET /recipes/{id}/cooks — list
# ---------------------------------------------------------------------------


def test_list_empty_returns_empty_envelope(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.get(f"/v1/recipes/{recipe_id}/cooks", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.get_json() == {"cooks": []}


def test_list_is_reverse_chronological(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    # Insert three logs out of date order.
    for day in ("2024-01-01", "2024-03-15", "2024-02-10"):
        resp = client.post(
            f"/v1/recipes/{recipe_id}/cooks",
            json={"cooked_at": day},
            headers=_auth_headers(),
        )
        assert resp.status_code == 201

    resp = client.get(f"/v1/recipes/{recipe_id}/cooks", headers=_auth_headers())
    assert resp.status_code == 200
    dates = [c["cooked_at"] for c in resp.get_json()["cooks"]]
    assert dates == ["2024-03-15", "2024-02-10", "2024-01-01"]


def test_list_same_date_breaks_tie_by_created_at_desc(client, app, two_users):
    """Two logs on the same cook date come back most-recently-recorded first."""
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])
        first = CookLog(
            recipe_id=recipe_id,
            user_id=two_users["owner_id"],
            cooked_at=datetime.date(2024, 5, 1),
            notes="first",
            created_at=datetime.datetime(2024, 5, 1, 8, 0, 0),
        )
        second = CookLog(
            recipe_id=recipe_id,
            user_id=two_users["owner_id"],
            cooked_at=datetime.date(2024, 5, 1),
            notes="second",
            created_at=datetime.datetime(2024, 5, 1, 20, 0, 0),
        )
        db.session.add_all([first, second])
        db.session.commit()

    resp = client.get(f"/v1/recipes/{recipe_id}/cooks", headers=_auth_headers())
    notes = [c["notes"] for c in resp.get_json()["cooks"]]
    assert notes == ["second", "first"]


# ---------------------------------------------------------------------------
# POST /recipes/{id}/cooks — create + stat recalculation
# ---------------------------------------------------------------------------


def test_create_minimal_returns_201_and_shape(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.post(
        f"/v1/recipes/{recipe_id}/cooks",
        json={"cooked_at": "2024-06-01"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert set(body) == {"id", "cooked_at", "notes", "rating", "photo_url", "created_at"}
    assert body["cooked_at"] == "2024-06-01"
    assert body["notes"] is None
    assert body["rating"] is None
    assert body["photo_url"] is None

    # Parent recipe: one cook, no rating -> avg_rating NULL.
    with app.app_context():
        count, avg = _recipe_stats(recipe_id)
        assert count == 1
        assert avg is None


def test_create_with_rating_sets_avg(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.post(
        f"/v1/recipes/{recipe_id}/cooks",
        json={"cooked_at": "2024-06-01", "rating": 4},
        headers=_auth_headers(),
    )
    assert resp.status_code == 201

    with app.app_context():
        count, avg = _recipe_stats(recipe_id)
        assert count == 1
        assert avg == 4.0


def test_create_multiple_avg_excludes_unrated(client, app, two_users):
    """cook_count counts all logs; avg_rating averages only rated ones."""
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    for payload in (
        {"cooked_at": "2024-06-01", "rating": 5},
        {"cooked_at": "2024-06-02", "rating": 2},
        {"cooked_at": "2024-06-03"},  # unrated — excluded from avg
    ):
        resp = client.post(
            f"/v1/recipes/{recipe_id}/cooks",
            json=payload,
            headers=_auth_headers(),
        )
        assert resp.status_code == 201

    with app.app_context():
        count, avg = _recipe_stats(recipe_id)
        assert count == 3  # all three logs counted
        assert avg == 3.5  # mean of 5 and 2 only


def test_create_rounds_avg_to_two_decimals(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    # Ratings 1, 2, 2 -> mean 1.666... -> 1.67 at 2dp.
    for rating in (1, 2, 2):
        client.post(
            f"/v1/recipes/{recipe_id}/cooks",
            json={"cooked_at": "2024-06-01", "rating": rating},
            headers=_auth_headers(),
        )

    with app.app_context():
        _, avg = _recipe_stats(recipe_id)
        assert avg == 1.67


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing cooked_at
        {"cooked_at": "not-a-date"},  # bad date format
        {"cooked_at": "2024-06-01", "rating": 0},  # rating below range
        {"cooked_at": "2024-06-01", "rating": 6},  # rating above range
        {"cooked_at": "2024-06-01", "mystery": "x"},  # unknown field
    ],
)
def test_create_invalid_body_returns_400(client, app, two_users, payload):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.post(
        f"/v1/recipes/{recipe_id}/cooks",
        json=payload,
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# PATCH /recipes/{id}/cooks/{cook_id}
# ---------------------------------------------------------------------------


def _create_cook(client, recipe_id, **body) -> str:
    body.setdefault("cooked_at", "2024-06-01")
    resp = client.post(
        f"/v1/recipes/{recipe_id}/cooks", json=body, headers=_auth_headers()
    )
    assert resp.status_code == 201
    return resp.get_json()["id"]


def test_patch_rating_change_recalculates_avg(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])
    cook_id = _create_cook(client, recipe_id, rating=2)

    with app.app_context():
        _, avg = _recipe_stats(recipe_id)
        assert avg == 2.0

    resp = client.patch(
        f"/v1/recipes/{recipe_id}/cooks/{cook_id}",
        json={"rating": 5},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["rating"] == 5

    with app.app_context():
        _, avg = _recipe_stats(recipe_id)
        assert avg == 5.0


def test_patch_notes_only_leaves_other_fields(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])
    cook_id = _create_cook(client, recipe_id, rating=3, notes="old")

    resp = client.patch(
        f"/v1/recipes/{recipe_id}/cooks/{cook_id}",
        json={"notes": "new notes"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["notes"] == "new notes"
    assert body["rating"] == 3  # untouched

    with app.app_context():
        _, avg = _recipe_stats(recipe_id)
        assert avg == 3.0  # unchanged


def test_patch_clear_rating_via_null_recomputes_avg(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])
    rated = _create_cook(client, recipe_id, cooked_at="2024-06-01", rating=4)
    _create_cook(client, recipe_id, cooked_at="2024-06-02", rating=2)

    with app.app_context():
        _, avg = _recipe_stats(recipe_id)
        assert avg == 3.0  # mean of 4 and 2

    # Clearing the rating on the first log drops it out of the average.
    resp = client.patch(
        f"/v1/recipes/{recipe_id}/cooks/{rated}",
        json={"rating": None},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["rating"] is None

    with app.app_context():
        count, avg = _recipe_stats(recipe_id)
        assert count == 2  # both logs still counted
        assert avg == 2.0  # only the remaining rated log


def test_patch_cooked_at_is_unknown_field_400(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])
    cook_id = _create_cook(client, recipe_id)

    resp = client.patch(
        f"/v1/recipes/{recipe_id}/cooks/{cook_id}",
        json={"cooked_at": "2024-07-01"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_patch_bad_rating_returns_400(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])
    cook_id = _create_cook(client, recipe_id)

    resp = client.patch(
        f"/v1/recipes/{recipe_id}/cooks/{cook_id}",
        json={"rating": 9},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


def test_patch_cook_not_on_recipe_returns_404(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.patch(
        f"/v1/recipes/{recipe_id}/cooks/{uuid.uuid4()}",
        json={"notes": "x"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /recipes/{id}/cooks/{cook_id}
# ---------------------------------------------------------------------------


def test_delete_returns_204_and_decrements_count(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])
    keep = _create_cook(client, recipe_id, cooked_at="2024-06-01", rating=4)
    drop = _create_cook(client, recipe_id, cooked_at="2024-06-02", rating=2)

    with app.app_context():
        count, avg = _recipe_stats(recipe_id)
        assert count == 2
        assert avg == 3.0

    resp = client.delete(
        f"/v1/recipes/{recipe_id}/cooks/{drop}", headers=_auth_headers()
    )
    assert resp.status_code == 204
    assert resp.data == b""

    with app.app_context():
        count, avg = _recipe_stats(recipe_id)
        assert count == 1
        assert avg == 4.0  # only the remaining rated log

    # The kept log is still listed; the dropped one is gone.
    listing = client.get(f"/v1/recipes/{recipe_id}/cooks", headers=_auth_headers())
    ids = [c["id"] for c in listing.get_json()["cooks"]]
    assert ids == [keep]


def test_delete_last_rated_log_sets_avg_null(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])
    only_rated = _create_cook(client, recipe_id, cooked_at="2024-06-01", rating=5)
    _create_cook(client, recipe_id, cooked_at="2024-06-02")  # unrated

    resp = client.delete(
        f"/v1/recipes/{recipe_id}/cooks/{only_rated}", headers=_auth_headers()
    )
    assert resp.status_code == 204

    with app.app_context():
        count, avg = _recipe_stats(recipe_id)
        assert count == 1  # the unrated log remains
        assert avg is None  # no rated logs left


def test_delete_missing_returns_404(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.delete(
        f"/v1/recipes/{recipe_id}/cooks/{uuid.uuid4()}", headers=_auth_headers()
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Photo wiring (shared upload flow) + derived photo_url
# ---------------------------------------------------------------------------


def test_photo_s3_key_round_trips_and_url_is_derived(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    key = "uploads/abc123/cook.jpg"
    resp = client.post(
        f"/v1/recipes/{recipe_id}/cooks",
        json={"cooked_at": "2024-06-01", "photo_s3_key": key},
        headers=_auth_headers(),
    )
    assert resp.status_code == 201
    photo_url = resp.get_json()["photo_url"]
    # Derived via s3_key_to_url: https URL that ends with the stored key.
    assert photo_url is not None
    assert photo_url.startswith("https://")
    assert photo_url.endswith(key)

    # The stored column holds the raw key (not the URL).
    with app.app_context():
        log = CookLog.query.filter_by(recipe_id=recipe_id).first()
        assert log.photo_s3_key == key


# ---------------------------------------------------------------------------
# Ownership / auth
# ---------------------------------------------------------------------------


def test_endpoints_on_non_owned_recipe_return_403(client, app, two_users):
    with app.app_context():
        other_recipe = _seed_recipe(two_users["other_id"])
        # A real cook log on the other user's recipe for the PATCH/DELETE paths.
        log = CookLog(
            recipe_id=other_recipe,
            user_id=two_users["other_id"],
            cooked_at=datetime.date(2024, 6, 1),
        )
        db.session.add(log)
        db.session.commit()
        log_id = log.id

    assert client.get(
        f"/v1/recipes/{other_recipe}/cooks", headers=_auth_headers()
    ).status_code == 403
    assert client.post(
        f"/v1/recipes/{other_recipe}/cooks",
        json={"cooked_at": "2024-06-01"},
        headers=_auth_headers(),
    ).status_code == 403
    assert client.patch(
        f"/v1/recipes/{other_recipe}/cooks/{log_id}",
        json={"notes": "x"},
        headers=_auth_headers(),
    ).status_code == 403
    assert client.delete(
        f"/v1/recipes/{other_recipe}/cooks/{log_id}", headers=_auth_headers()
    ).status_code == 403


def test_endpoints_on_missing_recipe_return_404(client, two_users):
    missing = uuid.uuid4()
    assert client.get(
        f"/v1/recipes/{missing}/cooks", headers=_auth_headers()
    ).status_code == 404
    assert client.post(
        f"/v1/recipes/{missing}/cooks",
        json={"cooked_at": "2024-06-01"},
        headers=_auth_headers(),
    ).status_code == 404


def test_endpoints_on_soft_deleted_recipe_return_404(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(
            two_users["owner_id"], deleted_at=datetime.datetime.utcnow()
        )

    assert client.get(
        f"/v1/recipes/{recipe_id}/cooks", headers=_auth_headers()
    ).status_code == 404


def test_endpoints_require_auth(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    assert client.get(f"/v1/recipes/{recipe_id}/cooks").status_code == 401
    assert client.post(
        f"/v1/recipes/{recipe_id}/cooks", json={"cooked_at": "2024-06-01"}
    ).status_code == 401
    assert client.patch(
        f"/v1/recipes/{recipe_id}/cooks/{uuid.uuid4()}", json={"notes": "x"}
    ).status_code == 401
    assert client.delete(
        f"/v1/recipes/{recipe_id}/cooks/{uuid.uuid4()}"
    ).status_code == 401


def test_cross_recipe_isolation(client, app, two_users):
    """A cook log under recipe A is not reachable via recipe B's path."""
    with app.app_context():
        recipe_a = _seed_recipe(two_users["owner_id"], title="A")
        recipe_b = _seed_recipe(two_users["owner_id"], title="B")
    cook_id = _create_cook(client, recipe_a, notes="belongs to A")

    # PATCH/DELETE via B's path -> 404 (not found on B).
    assert client.patch(
        f"/v1/recipes/{recipe_b}/cooks/{cook_id}",
        json={"notes": "x"},
        headers=_auth_headers(),
    ).status_code == 404
    assert client.delete(
        f"/v1/recipes/{recipe_b}/cooks/{cook_id}", headers=_auth_headers()
    ).status_code == 404

    # The log is untouched and still only on A.
    a_list = client.get(f"/v1/recipes/{recipe_a}/cooks", headers=_auth_headers())
    assert [c["id"] for c in a_list.get_json()["cooks"]] == [cook_id]
    b_list = client.get(f"/v1/recipes/{recipe_b}/cooks", headers=_auth_headers())
    assert b_list.get_json()["cooks"] == []


# ---------------------------------------------------------------------------
# Generative property test: stat recalculation
# ---------------------------------------------------------------------------


def _random_rating_multisets():
    """Yield many randomised rating multisets (seeded for reproducibility).

    Each element is a list whose entries are either ``None`` (an unrated log)
    or an integer rating in 1..5. The all-unrated and empty cases are included
    explicitly so the NULL-average boundary is always exercised.
    """
    rng = random.Random(4101)  # fixed seed → deterministic test runs
    choices = [None, 1, 2, 3, 4, 5]

    cases = [[], [None], [None, None, None]]
    for _ in range(60):
        length = rng.randint(1, 10)
        cases.append([rng.choice(choices) for _ in range(length)])
    return cases


def _expected_avg(ratings):
    """The expected ``avg_rating`` for a multiset: mean of non-null, 2dp, or None."""
    rated = [r for r in ratings if r is not None]
    if not rated:
        return None
    return float(
        (Decimal(sum(rated)) / Decimal(len(rated))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


@pytest.mark.parametrize(
    "ratings", _random_rating_multisets(), ids=itertools.count()
)
def test_recalculation_matches_mean_of_rated_logs(client, app, two_users, ratings):
    """For any multiset of (optional) ratings, cook_count == total logs and
    avg_rating == mean of the non-null ratings rounded to 2dp (NULL if none).

    Each parametrised case posts the whole multiset of cook logs to a fresh
    recipe and checks the recomputed denormalised stats on the parent.

    Validates: Requirements 4.1 (cook_count / avg_rating recalculation)
    """
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    for index, rating in enumerate(ratings):
        body = {"cooked_at": f"2024-06-{(index % 28) + 1:02d}"}
        if rating is not None:
            body["rating"] = rating
        resp = client.post(
            f"/v1/recipes/{recipe_id}/cooks", json=body, headers=_auth_headers()
        )
        assert resp.status_code == 201

    with app.app_context():
        count, avg = _recipe_stats(recipe_id)
        assert count == len(ratings)
        assert avg == _expected_avg(ratings)
