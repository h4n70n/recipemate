"""Tests for ``GET /v1/search`` — the heuristic filter search endpoint.

These exercise the contract from ``docs/api.md`` and ``TASK-3.1``:

* only the authenticated caller's own, non-soft-deleted recipes are searched,
* repeatable ``ingredient``/``tag``/``tool`` filters use ALL-match (AND)
  semantics within a field, and all filters AND together across fields,
* ``max_time`` filters by ``total_time_min`` (``<=``, excluding NULLs),
* ``q`` is a case-insensitive substring match on the title,
* name matching for ingredient/tag/tool is case-insensitive exact match,
* the four sort options order correctly (including NULL ``avg_rating`` last),
* ``total`` counts all matches and ``limit``/``offset`` page the result,
* bad ``sort`` -> 400, ``limit`` clamps to 100, and no auth -> 401,
* each item is the same recipe *card* shape as ``GET /recipes``.

Auth is stubbed exactly as in :mod:`tests.recipes.test_list_recipes`:
``validate_token`` is replaced with a trivial decoder and ``resolve_current_user``
is pointed at a seeded owner, so the tests never touch Cognito/JWKS. The
DB-backed ``app``/``client`` fixtures come from :mod:`tests.conftest`.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app import db
from app.auth import decorators
from app.models.recipe import Ingredient, Recipe, Tag, Tool
from app.models.user import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    """A bearer header; the token value is irrelevant under the stub."""
    return {"Authorization": "Bearer test-token"}


def _make_user(email: str, cognito_sub: str) -> User:
    user = User(email=email, cognito_sub=cognito_sub)
    db.session.add(user)
    db.session.commit()
    return user


def _make_recipe(
    user: User,
    *,
    title: str,
    created_at: datetime.datetime,
    deleted_at: datetime.datetime | None = None,
    total_time_min: int | None = None,
    cook_count: int = 0,
    avg_rating=None,
    thumbnail_s3_key: str | None = None,
    ingredients: list[str] | None = None,
    tools: list[str] | None = None,
    tags: list[Tag] | None = None,
) -> Recipe:
    recipe = Recipe(
        id=uuid.uuid4(),
        user_id=user.id,
        title=title,
        created_at=created_at,
        updated_at=created_at,
        deleted_at=deleted_at,
        total_time_min=total_time_min,
        cook_count=cook_count,
        avg_rating=avg_rating,
        thumbnail_s3_key=thumbnail_s3_key,
    )
    db.session.add(recipe)
    db.session.flush()

    for order, name in enumerate(ingredients or []):
        db.session.add(
            Ingredient(
                id=uuid.uuid4(),
                recipe_id=recipe.id,
                name=name,
                sort_order=order,
            )
        )
    for order, name in enumerate(tools or []):
        db.session.add(
            Tool(
                id=uuid.uuid4(),
                recipe_id=recipe.id,
                name=name,
                sort_order=order,
            )
        )
    if tags:
        recipe.tags.extend(tags)

    db.session.commit()
    return recipe


def _get_or_make_tag(name: str) -> Tag:
    existing = Tag.query.filter_by(name=name).first()
    if existing is not None:
        return existing
    tag = Tag(id=uuid.uuid4(), name=name)
    db.session.add(tag)
    db.session.commit()
    return tag


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def as_owner(app, monkeypatch):
    """Seed an owning user, wire the auth stubs, and yield seeding helpers."""
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

    base = datetime.datetime(2024, 1, 1, 12, 0, 0)

    def make(**kwargs):
        with app.app_context():
            owner = User.query.get(owner_id)
            # Resolve any tag names to shared Tag rows inside this context.
            tag_names = kwargs.pop("tag_names", None)
            if tag_names is not None:
                kwargs["tags"] = [_get_or_make_tag(n) for n in tag_names]
            return _make_recipe(owner, **kwargs)

    return {"owner_id": owner_id, "base": base, "make": make}


@pytest.fixture()
def dataset(app, as_owner):
    """A realistic seeded dataset of several owned recipes.

    Returns the ``as_owner`` dict so tests keep access to ``base`` and the
    owner id. Timestamps increase with each recipe so "newest" order is
    deterministic (``garlic_bread`` is newest).
    """
    base = as_owner["base"]
    day = datetime.timedelta(days=1)

    # Oldest -> newest.
    as_owner["make"](
        title="Chicken Soup",
        created_at=base,
        total_time_min=45,
        cook_count=1,
        avg_rating=3.0,
        ingredients=["Chicken", "Carrot", "Onion"],
        tools=["Pot"],
        tag_names=["comfort", "soup"],
    )
    as_owner["make"](
        title="Garlic Chicken",
        created_at=base + day,
        total_time_min=30,
        cook_count=5,
        avg_rating=4.5,
        ingredients=["Chicken", "Garlic", "Butter"],
        tools=["Cast Iron", "Pan"],
        tag_names=["quick", "weeknight"],
    )
    as_owner["make"](
        title="Pasta Primavera",
        created_at=base + 2 * day,
        total_time_min=25,
        cook_count=2,
        avg_rating=None,  # unrated
        ingredients=["Pasta", "Zucchini", "Garlic"],
        tools=["Pot"],
        tag_names=["quick", "vegetarian"],
    )
    as_owner["make"](
        title="Slow Brisket",
        created_at=base + 3 * day,
        total_time_min=480,
        cook_count=0,
        avg_rating=5.0,
        ingredients=["Beef", "Onion"],
        tools=["Smoker"],
        tag_names=["weekend"],
    )
    as_owner["make"](
        title="Garlic Bread",
        created_at=base + 4 * day,
        total_time_min=None,  # unknown total time
        cook_count=3,
        avg_rating=4.0,
        ingredients=["Bread", "Garlic", "Butter"],
        tools=["Oven"],
        tag_names=["quick", "side"],
    )
    return as_owner


def _titles(body) -> list[str]:
    return [r["title"] for r in body["recipes"]]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_requires_auth(client):
    resp = client.get("/v1/search")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# No filters
# ---------------------------------------------------------------------------


def test_no_filters_returns_all_owned_newest_first(client, dataset):
    resp = client.get("/v1/search", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 5
    assert _titles(body) == [
        "Garlic Bread",
        "Slow Brisket",
        "Pasta Primavera",
        "Garlic Chicken",
        "Chicken Soup",
    ]


# ---------------------------------------------------------------------------
# Ingredient filter (ALL)
# ---------------------------------------------------------------------------


def test_single_ingredient_filter(client, dataset):
    resp = client.get(
        "/v1/search?ingredient=chicken", headers=_auth_headers()
    )
    body = resp.get_json()
    assert body["total"] == 2
    assert set(_titles(body)) == {"Chicken Soup", "Garlic Chicken"}


def test_multiple_ingredients_require_all(client, dataset):
    # chicken AND garlic -> only Garlic Chicken (Chicken Soup lacks garlic).
    resp = client.get(
        "/v1/search?ingredient=chicken&ingredient=garlic",
        headers=_auth_headers(),
    )
    body = resp.get_json()
    assert body["total"] == 1
    assert _titles(body) == ["Garlic Chicken"]


def test_ingredient_name_match_is_case_insensitive(client, dataset):
    resp = client.get(
        "/v1/search?ingredient=CHICKEN", headers=_auth_headers()
    )
    body = resp.get_json()
    assert set(_titles(body)) == {"Chicken Soup", "Garlic Chicken"}


# ---------------------------------------------------------------------------
# Tag filter (ALL)
# ---------------------------------------------------------------------------


def test_multiple_tags_require_all(client, dataset):
    # quick AND vegetarian -> only Pasta Primavera.
    resp = client.get(
        "/v1/search?tag=quick&tag=vegetarian", headers=_auth_headers()
    )
    body = resp.get_json()
    assert body["total"] == 1
    assert _titles(body) == ["Pasta Primavera"]


def test_tag_name_match_is_case_insensitive(client, dataset):
    resp = client.get("/v1/search?tag=QUICK", headers=_auth_headers())
    body = resp.get_json()
    assert set(_titles(body)) == {
        "Garlic Chicken",
        "Pasta Primavera",
        "Garlic Bread",
    }


# ---------------------------------------------------------------------------
# Tool filter (ALL)
# ---------------------------------------------------------------------------


def test_multiple_tools_require_all(client, dataset):
    # cast iron AND pan -> only Garlic Chicken.
    resp = client.get(
        "/v1/search?tool=cast+iron&tool=pan", headers=_auth_headers()
    )
    body = resp.get_json()
    assert body["total"] == 1
    assert _titles(body) == ["Garlic Chicken"]


def test_tool_name_match_is_case_insensitive(client, dataset):
    resp = client.get("/v1/search?tool=POT", headers=_auth_headers())
    body = resp.get_json()
    assert set(_titles(body)) == {"Chicken Soup", "Pasta Primavera"}


# ---------------------------------------------------------------------------
# max_time
# ---------------------------------------------------------------------------


def test_max_time_boundary_inclusive_and_excludes_null(client, dataset):
    # <= 30 keeps Garlic Chicken (30) and Pasta Primavera (25); excludes the
    # 45/480 recipes and the NULL-total Garlic Bread.
    resp = client.get("/v1/search?max_time=30", headers=_auth_headers())
    body = resp.get_json()
    assert body["total"] == 2
    assert set(_titles(body)) == {"Garlic Chicken", "Pasta Primavera"}


# ---------------------------------------------------------------------------
# q (title substring, case-insensitive)
# ---------------------------------------------------------------------------


def test_q_case_insensitive_title_substring(client, dataset):
    resp = client.get("/v1/search?q=garlic", headers=_auth_headers())
    body = resp.get_json()
    # Newest-first: Garlic Bread then Garlic Chicken.
    assert _titles(body) == ["Garlic Bread", "Garlic Chicken"]


def test_q_matches_mid_string(client, dataset):
    resp = client.get("/v1/search?q=risk", headers=_auth_headers())
    body = resp.get_json()
    assert _titles(body) == ["Slow Brisket"]


# ---------------------------------------------------------------------------
# Combined filters AND together
# ---------------------------------------------------------------------------


def test_combined_filters_and_together(client, dataset):
    # garlic ingredient + quick tag + max_time 30:
    #   Garlic Chicken: garlic yes, quick yes, 30 <= 30 yes -> included
    #   Pasta Primavera: garlic yes, quick yes, 25 <= 30 yes -> included
    #   Garlic Bread: garlic yes, quick yes, but total_time NULL -> excluded
    resp = client.get(
        "/v1/search?ingredient=garlic&tag=quick&max_time=30",
        headers=_auth_headers(),
    )
    body = resp.get_json()
    assert body["total"] == 2
    assert set(_titles(body)) == {"Garlic Chicken", "Pasta Primavera"}


# ---------------------------------------------------------------------------
# Sort options
# ---------------------------------------------------------------------------


def test_sort_newest(client, dataset):
    resp = client.get("/v1/search?sort=newest", headers=_auth_headers())
    assert _titles(resp.get_json()) == [
        "Garlic Bread",
        "Slow Brisket",
        "Pasta Primavera",
        "Garlic Chicken",
        "Chicken Soup",
    ]


def test_sort_oldest(client, dataset):
    resp = client.get("/v1/search?sort=oldest", headers=_auth_headers())
    assert _titles(resp.get_json()) == [
        "Chicken Soup",
        "Garlic Chicken",
        "Pasta Primavera",
        "Slow Brisket",
        "Garlic Bread",
    ]


def test_sort_most_cooked(client, dataset):
    resp = client.get("/v1/search?sort=most_cooked", headers=_auth_headers())
    # cook_count desc: Garlic Chicken(5), Garlic Bread(3), Pasta(2),
    # Chicken Soup(1), Slow Brisket(0).
    assert _titles(resp.get_json()) == [
        "Garlic Chicken",
        "Garlic Bread",
        "Pasta Primavera",
        "Chicken Soup",
        "Slow Brisket",
    ]


def test_sort_highest_rated_nulls_last(client, dataset):
    resp = client.get(
        "/v1/search?sort=highest_rated", headers=_auth_headers()
    )
    # avg_rating desc with NULL last: Slow Brisket(5.0), Garlic Chicken(4.5),
    # Garlic Bread(4.0), Chicken Soup(3.0), then Pasta Primavera(NULL).
    assert _titles(resp.get_json()) == [
        "Slow Brisket",
        "Garlic Chicken",
        "Garlic Bread",
        "Chicken Soup",
        "Pasta Primavera",
    ]


# ---------------------------------------------------------------------------
# Pagination under filters
# ---------------------------------------------------------------------------


def test_pagination_with_total_under_filter(client, dataset):
    # quick tag matches 3 recipes; page them 2 at a time, newest-first.
    resp = client.get(
        "/v1/search?tag=quick&limit=2&offset=0", headers=_auth_headers()
    )
    body = resp.get_json()
    assert body["total"] == 3
    assert _titles(body) == ["Garlic Bread", "Pasta Primavera"]

    resp = client.get(
        "/v1/search?tag=quick&limit=2&offset=2", headers=_auth_headers()
    )
    body = resp.get_json()
    assert body["total"] == 3
    assert _titles(body) == ["Garlic Chicken"]


# ---------------------------------------------------------------------------
# Scope: only own, non-deleted recipes
# ---------------------------------------------------------------------------


def test_only_own_non_deleted_recipes_are_searched(client, app, dataset):
    base = dataset["base"]

    # Another user's recipe that would otherwise match everything.
    with app.app_context():
        other = _make_user("other@example.com", "sub-other")
        _make_recipe(
            other,
            title="Chicken Garlic (theirs)",
            created_at=base,
            total_time_min=10,
            ingredients=["Chicken", "Garlic"],
        )

    # A soft-deleted own recipe that matches the ingredient filter.
    dataset["make"](
        title="Deleted Garlic Chicken",
        created_at=base,
        deleted_at=datetime.datetime(2024, 2, 1),
        total_time_min=10,
        ingredients=["Chicken", "Garlic"],
    )

    resp = client.get(
        "/v1/search?ingredient=chicken&ingredient=garlic",
        headers=_auth_headers(),
    )
    body = resp.get_json()
    # Only the owner's live Garlic Chicken — neither the other user's nor the
    # soft-deleted recipe appears.
    assert body["total"] == 1
    assert _titles(body) == ["Garlic Chicken"]


# ---------------------------------------------------------------------------
# Validation / clamping
# ---------------------------------------------------------------------------


def test_invalid_sort_returns_400(client, dataset):
    resp = client.get("/v1/search?sort=bogus", headers=_auth_headers())
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_non_integer_max_time_returns_400(client, dataset):
    resp = client.get("/v1/search?max_time=soon", headers=_auth_headers())
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_negative_max_time_returns_400(client, dataset):
    resp = client.get("/v1/search?max_time=-5", headers=_auth_headers())
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_limit_clamped_to_100(client, dataset):
    resp = client.get("/v1/search?limit=100000", headers=_auth_headers())
    assert resp.status_code == 200
    # All 5 still returned (clamp does not error and the page is large enough).
    assert resp.get_json()["total"] == 5
    assert len(resp.get_json()["recipes"]) == 5


# ---------------------------------------------------------------------------
# Card shape
# ---------------------------------------------------------------------------


def test_card_shape_matches_list_endpoint(client, dataset):
    resp = client.get("/v1/search?q=pasta", headers=_auth_headers())
    card = resp.get_json()["recipes"][0]
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
    assert card["title"] == "Pasta Primavera"
    assert sorted(card["tags"]) == ["quick", "vegetarian"]
