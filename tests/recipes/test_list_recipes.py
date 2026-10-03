"""Tests for ``GET /v1/recipes`` — the paginated recipe list endpoint.

These exercise the endpoint's contract from ``docs/api.md``:

* only the authenticated user's recipes are returned,
* soft-deleted recipes (``deleted_at`` set) are excluded,
* ``total`` is the full count ignoring pagination,
* ``limit``/``offset`` page the newest-first result set,
* ``limit`` is clamped to a maximum of 100,
* each item is a recipe *card* with the documented fields.

Auth is stubbed the same way as :mod:`tests.auth.test_require_auth_scopes`:
``validate_token`` is replaced with a trivial decoder and ``resolve_current_user``
is pointed at a seeded :class:`~app.models.user.User`, so the tests never touch
Cognito/JWKS and stay focused on the list behaviour. The DB-backed ``app`` and
``client`` fixtures come from :mod:`tests.conftest`.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app import db
from app.auth import decorators
from app.models.recipe import Recipe, Tag
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
    if tags:
        recipe.tags.extend(tags)
    db.session.commit()
    return recipe


@pytest.fixture()
def as_owner(app, monkeypatch):
    """Seed an owning user and wire the auth stubs; yield helpers.

    Yields a dict with ``user_id`` plus a ``make_recipe`` factory bound to the
    owner, and seeds everything inside the app context so later requests see it.
    """
    with app.app_context():
        owner = _make_user("owner@example.com", "sub-owner")
        owner_id = owner.id

    monkeypatch.setattr(
        decorators, "validate_token", lambda token: {"sub": "sub-owner"}
    )
    monkeypatch.setattr(
        decorators, "resolve_current_user", lambda claims: User.query.get(owner_id)
    )

    base = datetime.datetime(2024, 1, 1, 12, 0, 0)

    def make(**kwargs):
        with app.app_context():
            owner = User.query.get(owner_id)
            return _make_recipe(owner, **kwargs)

    return {"owner_id": owner_id, "base": base, "make": make}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_list_returns_empty_recipes_and_zero_total(client, as_owner):
    resp = client.get("/v1/recipes", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"recipes": [], "total": 0}


def test_requires_auth(client):
    resp = client.get("/v1/recipes")
    assert resp.status_code == 401


def test_returns_only_own_recipes(client, app, as_owner, monkeypatch):
    base = as_owner["base"]
    as_owner["make"](title="Mine", created_at=base)

    # A different user with their own recipe.
    with app.app_context():
        other = _make_user("other@example.com", "sub-other")
        _make_recipe(other, title="Theirs", created_at=base)

    resp = client.get("/v1/recipes", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 1
    titles = [r["title"] for r in body["recipes"]]
    assert titles == ["Mine"]


def test_excludes_soft_deleted(client, as_owner):
    base = as_owner["base"]
    as_owner["make"](title="Live", created_at=base)
    as_owner["make"](
        title="Deleted",
        created_at=base,
        deleted_at=datetime.datetime(2024, 2, 1),
    )

    resp = client.get("/v1/recipes", headers=_auth_headers())
    body = resp.get_json()
    assert body["total"] == 1
    assert [r["title"] for r in body["recipes"]] == ["Live"]


def test_ordered_newest_first(client, as_owner):
    base = as_owner["base"]
    as_owner["make"](title="Oldest", created_at=base)
    as_owner["make"](
        title="Newest", created_at=base + datetime.timedelta(days=2)
    )
    as_owner["make"](
        title="Middle", created_at=base + datetime.timedelta(days=1)
    )

    resp = client.get("/v1/recipes", headers=_auth_headers())
    titles = [r["title"] for r in resp.get_json()["recipes"]]
    assert titles == ["Newest", "Middle", "Oldest"]


def test_pagination_limit_and_offset(client, as_owner):
    base = as_owner["base"]
    # Create 5 recipes with increasing timestamps -> r4 newest.
    for i in range(5):
        as_owner["make"](
            title=f"r{i}", created_at=base + datetime.timedelta(days=i)
        )

    # First page: limit 2 -> two newest.
    resp = client.get("/v1/recipes?limit=2&offset=0", headers=_auth_headers())
    body = resp.get_json()
    assert body["total"] == 5
    assert [r["title"] for r in body["recipes"]] == ["r4", "r3"]

    # Second page.
    resp = client.get("/v1/recipes?limit=2&offset=2", headers=_auth_headers())
    body = resp.get_json()
    assert body["total"] == 5
    assert [r["title"] for r in body["recipes"]] == ["r2", "r1"]

    # Last partial page.
    resp = client.get("/v1/recipes?limit=2&offset=4", headers=_auth_headers())
    body = resp.get_json()
    assert [r["title"] for r in body["recipes"]] == ["r0"]


def test_limit_clamped_to_100(client, as_owner):
    base = as_owner["base"]
    as_owner["make"](title="only", created_at=base)

    # Request a huge limit; clamp should not error and should still return rows.
    resp = client.get("/v1/recipes?limit=100000", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.get_json()["total"] == 1


def test_card_shape_and_fields(client, app, as_owner):
    base = as_owner["base"]
    with app.app_context():
        tag_a = Tag(id=uuid.uuid4(), name="Italian")
        tag_b = Tag(id=uuid.uuid4(), name="Quick")
        db.session.add_all([tag_a, tag_b])
        db.session.commit()
        tag_a_id, tag_b_id = tag_a.id, tag_b.id

    with app.app_context():
        tags = [Tag.query.get(tag_a_id), Tag.query.get(tag_b_id)]
        owner = User.query.get(as_owner["owner_id"])
        _make_recipe(
            owner,
            title="Carbonara",
            created_at=base,
            total_time_min=25,
            cook_count=3,
            avg_rating=4.5,
            thumbnail_s3_key="thumbnails/abc.jpg",
            tags=tags,
        )

    resp = client.get("/v1/recipes", headers=_auth_headers())
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
    assert card["title"] == "Carbonara"
    assert card["total_time_min"] == 25
    assert card["cook_count"] == 3
    assert card["avg_rating"] == 4.5
    assert sorted(card["tags"]) == ["Italian", "Quick"]
    # Default (no CloudFront) -> direct S3 URL ending in the key.
    assert card["thumbnail_url"].endswith("thumbnails/abc.jpg")
    assert card["thumbnail_url"].startswith("https://")


def test_null_thumbnail_serialises_as_none(client, as_owner):
    base = as_owner["base"]
    as_owner["make"](title="no-thumb", created_at=base, thumbnail_s3_key=None)

    resp = client.get("/v1/recipes", headers=_auth_headers())
    card = resp.get_json()["recipes"][0]
    assert card["thumbnail_url"] is None
