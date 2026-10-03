"""Tests for the tag endpoints (TASK-2.4).

Covers the three tag endpoints documented in ``docs/api.md`` under *Tags*:

* ``POST /v1/recipes/{id}/tags`` — add a tag to a recipe (find-or-create the
  global tag, idempotent association),
* ``DELETE /v1/recipes/{id}/tags/{tag_id}`` — remove the association only,
  leaving the shared global tag row intact,
* ``GET /v1/tags`` — list the distinct tags attached to the caller's own,
  non-soft-deleted recipes, name-ordered.

The suite also confirms a newly added tag is visible in both
``GET /recipes/{id}`` (the ``tags`` name array) and the ``GET /recipes`` card.

Design facts under test (from the task + model):

* ``tags.name`` is globally unique, so adding a name another user already
  introduced reuses the same :class:`Tag` row (same id).
* ``GET /tags`` is user-scoped and must exclude tags only used by other users
  and tags whose sole association is a soft-deleted recipe.

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
from app.models.recipe import Recipe, RecipeTag, Tag
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


@pytest.fixture()
def two_users(app, monkeypatch):
    """Seed an owner and a second user; auth resolves to the owner.

    Yields both ids. ``resolve_current_user`` is wired to the owner so every
    request via ``client`` is authenticated as the owner; the ``other`` user is
    a real, distinct row recipes/tags can be assigned to for the cross-user
    cases.
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
# POST /recipes/{id}/tags — add
# ---------------------------------------------------------------------------


def test_add_new_tag_creates_global_tag_and_association(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.post(
        f"/v1/recipes/{recipe_id}/tags",
        json={"name": "Italian"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "Italian"
    tag_id = body["id"]

    # The global tag and the association both exist.
    with app.app_context():
        tag = Tag.query.filter_by(name="Italian").first()
        assert tag is not None and str(tag.id) == tag_id
        assert (
            RecipeTag.query.filter_by(recipe_id=recipe_id, tag_id=tag.id).first()
            is not None
        )


def test_added_tag_appears_in_recipe_detail_and_card(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    client.post(
        f"/v1/recipes/{recipe_id}/tags",
        json={"name": "Italian"},
        headers=_auth_headers(),
    )

    # GET /recipes/{id} shows the tag name.
    detail = client.get(f"/v1/recipes/{recipe_id}", headers=_auth_headers())
    assert detail.status_code == 200
    assert "Italian" in detail.get_json()["tags"]

    # The card in GET /recipes shows it too.
    listing = client.get("/v1/recipes", headers=_auth_headers())
    assert listing.status_code == 200
    cards = listing.get_json()["recipes"]
    card = next(c for c in cards if c["id"] == str(recipe_id))
    assert "Italian" in card["tags"]


def test_add_same_tag_twice_is_idempotent(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    first = client.post(
        f"/v1/recipes/{recipe_id}/tags",
        json={"name": "Italian"},
        headers=_auth_headers(),
    )
    assert first.status_code == 201

    # Second add of the same tag: still succeeds, 200 (already present),
    # same tag id, and no duplicate association row.
    second = client.post(
        f"/v1/recipes/{recipe_id}/tags",
        json={"name": "Italian"},
        headers=_auth_headers(),
    )
    assert second.status_code == 200
    assert second.get_json()["id"] == first.get_json()["id"]

    with app.app_context():
        tag = Tag.query.filter_by(name="Italian").first()
        assert (
            RecipeTag.query.filter_by(
                recipe_id=recipe_id, tag_id=tag.id
            ).count()
            == 1
        )


def test_add_tag_reuses_existing_global_tag_from_another_user(
    client, app, two_users
):
    """A name another user's recipe already uses reuses the same Tag row."""
    with app.app_context():
        # Another user's recipe already carries the "Italian" tag.
        other_recipe_id = _seed_recipe(two_users["other_id"])
        tag = Tag(name="Italian")
        db.session.add(tag)
        db.session.flush()
        db.session.add(RecipeTag(recipe_id=other_recipe_id, tag_id=tag.id))
        db.session.commit()
        existing_tag_id = str(tag.id)

        my_recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.post(
        f"/v1/recipes/{my_recipe_id}/tags",
        json={"name": "Italian"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 201
    # Same global tag row is reused — id matches the pre-existing one.
    assert resp.get_json()["id"] == existing_tag_id

    with app.app_context():
        # Exactly one global "Italian" tag, now associated with both recipes.
        assert Tag.query.filter_by(name="Italian").count() == 1
        assert (
            RecipeTag.query.filter_by(tag_id=uuid.UUID(existing_tag_id)).count()
            == 2
        )


def test_add_tag_strips_whitespace(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.post(
        f"/v1/recipes/{recipe_id}/tags",
        json={"name": "  Italian  "},
        headers=_auth_headers(),
    )
    assert resp.status_code == 201
    assert resp.get_json()["name"] == "Italian"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "   "},
        {},  # missing name
        {"name": "Italian", "colour": "green"},  # unknown field
    ],
)
def test_add_tag_invalid_body_returns_400(client, app, two_users, payload):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.post(
        f"/v1/recipes/{recipe_id}/tags",
        json=payload,
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_add_tag_over_length_returns_400(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.post(
        f"/v1/recipes/{recipe_id}/tags",
        json={"name": "x" * 101},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_add_tag_on_non_owned_recipe_returns_403(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["other_id"])

    resp = client.post(
        f"/v1/recipes/{recipe_id}/tags",
        json={"name": "Italian"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 403
    assert "error" in resp.get_json()


def test_add_tag_on_missing_recipe_returns_404(client, two_users):
    resp = client.post(
        f"/v1/recipes/{uuid.uuid4()}/tags",
        json={"name": "Italian"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404


def test_add_tag_on_soft_deleted_recipe_returns_404(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(
            two_users["owner_id"], deleted_at=datetime.datetime.utcnow()
        )

    resp = client.post(
        f"/v1/recipes/{recipe_id}/tags",
        json={"name": "Italian"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404


def test_add_tag_requires_auth(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.post(
        f"/v1/recipes/{recipe_id}/tags", json={"name": "Italian"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /recipes/{id}/tags/{tag_id} — remove association only
# ---------------------------------------------------------------------------


def test_delete_association_returns_204_and_keeps_global_tag(
    client, app, two_users
):
    """Removing the association leaves the shared global tag (and other
    associations) intact."""
    with app.app_context():
        # The owner's recipe and another user's recipe both carry the tag.
        my_recipe_id = _seed_recipe(two_users["owner_id"])
        other_recipe_id = _seed_recipe(two_users["other_id"])
        tag = Tag(name="Italian")
        db.session.add(tag)
        db.session.flush()
        db.session.add_all(
            [
                RecipeTag(recipe_id=my_recipe_id, tag_id=tag.id),
                RecipeTag(recipe_id=other_recipe_id, tag_id=tag.id),
            ]
        )
        db.session.commit()
        tag_id = tag.id

    resp = client.delete(
        f"/v1/recipes/{my_recipe_id}/tags/{tag_id}", headers=_auth_headers()
    )
    assert resp.status_code == 204
    assert resp.data == b""

    with app.app_context():
        # Association on the owner's recipe is gone.
        assert (
            RecipeTag.query.filter_by(
                recipe_id=my_recipe_id, tag_id=tag_id
            ).first()
            is None
        )
        # Global tag row still exists, and the other user's association remains.
        assert Tag.query.get(tag_id) is not None
        assert (
            RecipeTag.query.filter_by(
                recipe_id=other_recipe_id, tag_id=tag_id
            ).first()
            is not None
        )

    # The tag no longer appears on the owner's recipe detail.
    detail = client.get(f"/v1/recipes/{my_recipe_id}", headers=_auth_headers())
    assert "Italian" not in detail.get_json()["tags"]


def test_delete_tag_not_on_recipe_returns_404(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])
        # A real global tag that is NOT attached to this recipe.
        tag = Tag(name="Italian")
        db.session.add(tag)
        db.session.commit()
        tag_id = tag.id

    resp = client.delete(
        f"/v1/recipes/{recipe_id}/tags/{tag_id}", headers=_auth_headers()
    )
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_delete_unknown_tag_id_returns_404(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.delete(
        f"/v1/recipes/{recipe_id}/tags/{uuid.uuid4()}", headers=_auth_headers()
    )
    assert resp.status_code == 404


def test_delete_tag_on_non_owned_recipe_returns_403(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["other_id"])
        tag = Tag(name="Italian")
        db.session.add(tag)
        db.session.flush()
        db.session.add(RecipeTag(recipe_id=recipe_id, tag_id=tag.id))
        db.session.commit()
        tag_id = tag.id

    resp = client.delete(
        f"/v1/recipes/{recipe_id}/tags/{tag_id}", headers=_auth_headers()
    )
    assert resp.status_code == 403

    # The non-owner's association is untouched.
    with app.app_context():
        assert (
            RecipeTag.query.filter_by(recipe_id=recipe_id, tag_id=tag_id).first()
            is not None
        )


def test_delete_tag_requires_auth(client, app, two_users):
    with app.app_context():
        recipe_id = _seed_recipe(two_users["owner_id"])

    resp = client.delete(f"/v1/recipes/{recipe_id}/tags/{uuid.uuid4()}")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /tags — user-scoped distinct listing
# ---------------------------------------------------------------------------


def test_list_tags_returns_only_callers_distinct_tags_name_ordered(
    client, app, two_users
):
    with app.app_context():
        # Owner has two recipes; "shared" is used on both (must dedupe).
        r1 = _seed_recipe(two_users["owner_id"], title="R1")
        r2 = _seed_recipe(two_users["owner_id"], title="R2")

        zesty = Tag(name="Zesty")
        quick = Tag(name="Quick")
        shared = Tag(name="Shared")
        # A tag used ONLY by the other user — must not appear.
        other_only = Tag(name="OtherOnly")
        db.session.add_all([zesty, quick, shared, other_only])
        db.session.flush()

        other_recipe = _seed_recipe(two_users["other_id"], title="Theirs")
        db.session.add_all(
            [
                RecipeTag(recipe_id=r1, tag_id=zesty.id),
                RecipeTag(recipe_id=r1, tag_id=shared.id),
                RecipeTag(recipe_id=r2, tag_id=quick.id),
                RecipeTag(recipe_id=r2, tag_id=shared.id),
                RecipeTag(recipe_id=other_recipe, tag_id=other_only.id),
            ]
        )
        db.session.commit()

    resp = client.get("/v1/tags", headers=_auth_headers())
    assert resp.status_code == 200
    tags = resp.get_json()["tags"]

    names = [t["name"] for t in tags]
    # Distinct, caller-only, name-ordered. "OtherOnly" is excluded.
    assert names == ["Quick", "Shared", "Zesty"]
    # Each item carries id + name.
    assert all(set(t) == {"id", "name"} for t in tags)


def test_list_tags_excludes_tags_only_on_soft_deleted_recipes(
    client, app, two_users
):
    with app.app_context():
        live_recipe = _seed_recipe(two_users["owner_id"], title="Live")
        deleted_recipe = _seed_recipe(
            two_users["owner_id"],
            title="Deleted",
            deleted_at=datetime.datetime.utcnow(),
        )

        keep = Tag(name="Keep")
        gone = Tag(name="Gone")
        db.session.add_all([keep, gone])
        db.session.flush()
        db.session.add_all(
            [
                RecipeTag(recipe_id=live_recipe, tag_id=keep.id),
                RecipeTag(recipe_id=deleted_recipe, tag_id=gone.id),
            ]
        )
        db.session.commit()

    resp = client.get("/v1/tags", headers=_auth_headers())
    assert resp.status_code == 200
    names = [t["name"] for t in resp.get_json()["tags"]]
    assert names == ["Keep"]


def test_list_tags_empty_when_no_tags(client, app, two_users):
    with app.app_context():
        _seed_recipe(two_users["owner_id"])

    resp = client.get("/v1/tags", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.get_json() == {"tags": []}


def test_list_tags_requires_auth(client):
    resp = client.get("/v1/tags")
    assert resp.status_code == 401
