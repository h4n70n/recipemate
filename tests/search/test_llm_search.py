"""Tests for ``POST /v1/search/llm`` — AI natural-language recipe search.

These exercise the endpoint's contract from ``docs/api.md`` under
``POST /search/llm`` and the TASK-3.2 subtasks:

* a valid query returns 200 with recipes ranked in the LLM's order, each
  carrying a ``match_explanation``; only the caller's own recipes appear and
  ids the model invented are ignored,
* an empty / missing ``query`` or an unknown field returns 400, and an
  unauthenticated request returns 401,
* a user with no recipes gets ``{"recipes": []}`` *without* the LLM being
  called,
* caching: the first call invokes the LLM and stores the serialized result
  under the sha256 key with a 300s TTL; an identical second call is served from
  cache with no second LLM call,
* fallback: when the LLM seam raises (or returns malformed JSON) the endpoint
  still returns 200 via the self-contained heuristic, and the fallback result
  is NOT cached (an identical second call re-invokes the LLM).

The single OpenAI-call seam ``app.services.llm_search.call_llm_rankings`` is
monkeypatched so the network is never touched. Redis is replaced by an
in-memory stub that monkeypatches ``app.services.redis_client.get_redis_client``
(mirroring :mod:`tests.recipes.test_upload`). Auth is stubbed exactly like the
recipe tests.
"""

from __future__ import annotations

import datetime
import hashlib
import json

import pytest

from app import db
from app.auth import decorators
from app.models.recipe import Ingredient, Recipe, RecipeTag, Tag
from app.models.user import User
from app.services import llm_search, redis_client


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal in-memory stand-in for a redis-py client (get/set + TTL)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True


def _auth_headers() -> dict[str, str]:
    """A bearer header; the token value is irrelevant under the stub."""
    return {"Authorization": "Bearer test-token"}


def _make_user(email: str, cognito_sub: str) -> User:
    user = User(email=email, cognito_sub=cognito_sub)
    db.session.add(user)
    db.session.commit()
    return user


def _make_recipe(
    user_id,
    title: str,
    *,
    ingredients: list[str] | None = None,
    tags: list[str] | None = None,
    created_at: datetime.datetime | None = None,
    thumbnail_s3_key: str | None = None,
) -> Recipe:
    """Create a recipe (optionally with ingredient and tag rows) and commit."""
    recipe = Recipe(
        user_id=user_id,
        title=title,
        thumbnail_s3_key=thumbnail_s3_key,
        created_at=created_at or datetime.datetime.utcnow(),
    )
    db.session.add(recipe)
    db.session.flush()

    for order, name in enumerate(ingredients or []):
        db.session.add(
            Ingredient(recipe_id=recipe.id, name=name, sort_order=order)
        )

    for name in tags or []:
        tag = Tag.query.filter_by(name=name).first()
        if tag is None:
            tag = Tag(name=name)
            db.session.add(tag)
            db.session.flush()
        db.session.add(RecipeTag(recipe_id=recipe.id, tag_id=tag.id))

    db.session.commit()
    return recipe


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_redis(monkeypatch):
    """Point the cache helpers at an in-memory Redis stub; return it."""
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fake)
    return fake


@pytest.fixture()
def owner_env(app, monkeypatch):
    """Seed an owning user + wire the auth stubs; return seeded ids.

    Creates the caller's recipes (chicken, pasta) plus a recipe owned by a
    *different* user, so tests can assert cross-user isolation. Ids are
    captured as strings for convenient assertions.
    """
    with app.app_context():
        owner = _make_user("owner@example.com", "sub-owner")
        other = _make_user("other@example.com", "sub-other")
        owner_id = owner.id
        other_id = other.id

        base = datetime.datetime(2024, 1, 1, 12, 0, 0)
        chicken = _make_recipe(
            owner_id,
            "Lemon Garlic Chicken Thighs",
            ingredients=["chicken thighs", "garlic", "lemon"],
            tags=["dinner"],
            created_at=base,
            thumbnail_s3_key="thumbnails/chicken.jpg",
        )
        pasta = _make_recipe(
            owner_id,
            "Garlic Butter Pasta",
            ingredients=["pasta", "garlic", "butter"],
            tags=["quick"],
            created_at=base + datetime.timedelta(days=1),
        )
        # A recipe owned by someone else — must never leak into results.
        other_recipe = _make_recipe(
            other_id,
            "Someone Else's Chicken",
            ingredients=["chicken", "garlic"],
            created_at=base,
        )

        ids = {
            "owner_id": owner_id,
            "other_id": other_id,
            "chicken_id": str(chicken.id),
            "pasta_id": str(pasta.id),
            "other_recipe_id": str(other_recipe.id),
        }

    monkeypatch.setattr(
        decorators, "validate_token", lambda token: {"sub": "sub-owner"}
    )
    monkeypatch.setattr(
        decorators,
        "resolve_current_user",
        lambda claims: User.query.get(ids["owner_id"]),
    )
    return ids


class _LlmSpy:
    """Records calls to the LLM seam and returns a canned ranking."""

    def __init__(self, rankings):
        self.rankings = rankings
        self.calls = 0

    def __call__(self, query, index):
        self.calls += 1
        return list(self.rankings)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_query_returns_ranked_cards_with_explanations(
    client, owner_env, fake_redis, monkeypatch
):
    # Model ranks pasta first, then chicken, and also invents an id that must
    # be dropped.
    spy = _LlmSpy(
        [
            {"recipe_id": owner_env["pasta_id"], "match_explanation": "Lots of garlic."},
            {
                "recipe_id": owner_env["chicken_id"],
                "match_explanation": "Has chicken, garlic and lemon.",
            },
            {"recipe_id": "00000000-0000-0000-0000-000000000000",
             "match_explanation": "hallucinated"},
        ]
    )
    monkeypatch.setattr(llm_search, "call_llm_rankings", spy)

    resp = client.post(
        "/v1/search/llm",
        json={"query": "I have chicken thighs, garlic, and a lemon"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    recipes = body["recipes"]

    # LLM order preserved: pasta then chicken. Hallucinated id dropped.
    assert [r["id"] for r in recipes] == [
        owner_env["pasta_id"],
        owner_env["chicken_id"],
    ]
    assert recipes[0]["match_explanation"] == "Lots of garlic."
    assert recipes[1]["match_explanation"] == "Has chicken, garlic and lemon."

    # Cross-user recipe never appears.
    assert owner_env["other_recipe_id"] not in {r["id"] for r in recipes}

    # Card shape carries the derived thumbnail URL.
    chicken_card = recipes[1]
    assert chicken_card["title"] == "Lemon Garlic Chicken Thighs"
    assert chicken_card["thumbnail_url"].endswith("thumbnails/chicken.jpg")


def test_hallucinated_only_ranking_yields_empty_recipes(
    client, owner_env, fake_redis, monkeypatch
):
    spy = _LlmSpy(
        [{"recipe_id": "11111111-1111-1111-1111-111111111111",
          "match_explanation": "nope"}]
    )
    monkeypatch.setattr(llm_search, "call_llm_rankings", spy)

    resp = client.post(
        "/v1/search/llm",
        json={"query": "anything"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"recipes": []}
    assert spy.calls == 1


# ---------------------------------------------------------------------------
# Validation (400) / auth (401)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},                       # missing query
        {"query": ""},            # empty
        {"query": "   "},         # whitespace only
        {"query": "x", "extra": 1},  # unknown field
    ],
)
def test_invalid_body_returns_400(client, owner_env, fake_redis, payload):
    resp = client.post(
        "/v1/search/llm",
        json=payload,
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_requires_auth(client):
    resp = client.post("/v1/search/llm", json={"query": "chicken"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# No recipes short-circuit
# ---------------------------------------------------------------------------


def test_user_with_no_recipes_returns_empty_without_calling_llm(
    app, client, fake_redis, monkeypatch
):
    with app.app_context():
        empty_user = _make_user("empty@example.com", "sub-empty")
        empty_id = empty_user.id

    monkeypatch.setattr(
        decorators, "validate_token", lambda token: {"sub": "sub-empty"}
    )
    monkeypatch.setattr(
        decorators,
        "resolve_current_user",
        lambda claims: User.query.get(empty_id),
    )

    spy = _LlmSpy([])
    monkeypatch.setattr(llm_search, "call_llm_rankings", spy)

    resp = client.post(
        "/v1/search/llm",
        json={"query": "whatever"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"recipes": []}
    assert spy.calls == 0  # LLM not consulted when there is nothing to rank


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_result_is_cached_and_second_call_skips_llm(
    client, owner_env, fake_redis, monkeypatch
):
    spy = _LlmSpy(
        [{"recipe_id": owner_env["chicken_id"], "match_explanation": "chicken!"}]
    )
    monkeypatch.setattr(llm_search, "call_llm_rankings", spy)

    query = "chicken please"
    first = client.post(
        "/v1/search/llm", json={"query": query}, headers=_auth_headers()
    )
    assert first.status_code == 200
    assert spy.calls == 1

    # Stored under llmsearch:{user_id}:{sha256(strip+lower)} with 300s TTL.
    digest = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()
    expected_key = f"llmsearch:{owner_env['owner_id']}:{digest}"
    assert expected_key in fake_redis.store
    assert fake_redis.ttls[expected_key] == 300
    assert json.loads(fake_redis.store[expected_key]) == first.get_json()

    # Second identical call: served from cache, no second LLM call.
    second = client.post(
        "/v1/search/llm", json={"query": query}, headers=_auth_headers()
    )
    assert second.status_code == 200
    assert second.get_json() == first.get_json()
    assert spy.calls == 1


def test_cache_key_normalizes_case_and_whitespace(
    client, owner_env, fake_redis, monkeypatch
):
    spy = _LlmSpy(
        [{"recipe_id": owner_env["chicken_id"], "match_explanation": "chicken!"}]
    )
    monkeypatch.setattr(llm_search, "call_llm_rankings", spy)

    client.post(
        "/v1/search/llm", json={"query": "Chicken Please"}, headers=_auth_headers()
    )
    assert spy.calls == 1

    # Different surface form, same normalized query -> cache hit, no new call.
    client.post(
        "/v1/search/llm", json={"query": "  chicken please  "}, headers=_auth_headers()
    )
    assert spy.calls == 1


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


def test_llm_exception_falls_back_to_heuristic(
    client, owner_env, fake_redis, monkeypatch
):
    def _boom(query, index):
        raise RuntimeError("openai is down")

    calls = {"n": 0}

    def _counting_boom(query, index):
        calls["n"] += 1
        return _boom(query, index)

    monkeypatch.setattr(llm_search, "call_llm_rankings", _counting_boom)

    resp = client.post(
        "/v1/search/llm",
        json={"query": "chicken garlic"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    recipes = resp.get_json()["recipes"]

    # Heuristic still finds the matching owned recipes, with a "Matched on:"
    # explanation, and never the other user's recipe.
    ids = {r["id"] for r in recipes}
    assert owner_env["chicken_id"] in ids
    assert owner_env["other_recipe_id"] not in ids
    assert all(
        r["match_explanation"].startswith("Matched on:") for r in recipes
    )

    # Fallback result is NOT cached: a second identical call re-invokes the LLM.
    assert calls["n"] == 1
    resp2 = client.post(
        "/v1/search/llm",
        json={"query": "chicken garlic"},
        headers=_auth_headers(),
    )
    assert resp2.status_code == 200
    assert calls["n"] == 2


def test_malformed_llm_json_falls_back(
    client, owner_env, fake_redis, monkeypatch
):
    # Drive the real call_llm_rankings through a fake OpenAI client whose
    # content is not valid JSON, so _parse_rankings raises and we fall back.
    class _FakeMessage:
        content = "this is not json at all"

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeCompletion:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        def create(self, **kwargs):
            return _FakeCompletion()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(llm_search, "_get_openai_client", lambda: _FakeClient())

    resp = client.post(
        "/v1/search/llm",
        json={"query": "chicken garlic"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    recipes = resp.get_json()["recipes"]
    assert owner_env["chicken_id"] in {r["id"] for r in recipes}
    assert all(
        r["match_explanation"].startswith("Matched on:") for r in recipes
    )
    # Malformed-response fallback is not cached.
    digest = hashlib.sha256("chicken garlic".encode("utf-8")).hexdigest()
    assert f"llmsearch:{owner_env['owner_id']}:{digest}" not in fake_redis.store
