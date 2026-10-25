"""Rate-limit tests for ``POST /v1/search/llm``.

Complements :mod:`tests.search.test_llm_search` by exercising the per-user
fixed-window limiter wired into the endpoint (FEAT-002 of OR-5):

* requests under the configured limit return 200,
* the request that exceeds the limit returns 429 WITH a ``Retry-After``
  header and a concise ``{"error": ...}`` body,
* a Redis outage lets the request through (the limiter fails open).

The limit is lowered via ``app.config`` for the duration of a test. Redis is
replaced by an in-memory stub implementing the limiter surface
(``incr``/``expire``/``ttl``), monkeypatched onto
``app.services.redis_client.get_redis_client`` exactly like the sibling tests.
The LLM seam is stubbed so no network is touched, and auth is stubbed to a
seeded owner.
"""

from __future__ import annotations

import datetime

import pytest
import redis

from app import db
from app.auth import decorators
from app.models.recipe import Recipe
from app.models.user import User
from app.services import llm_search, redis_client


class _FakeRedis:
    """In-memory redis-py stand-in supporting caching + the limiter surface."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.counts: dict[str, int] = {}

    # llm_search caching surface
    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    # rate-limit surface
    def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    def ttl(self, key: str) -> int:
        if key not in self.counts:
            return -2
        return self.ttls.get(key, -1)


class _ExplodingRedis:
    """Redis stub whose every operation raises, simulating an outage."""

    def get(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")

    def set(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")

    def incr(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")

    def expire(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")

    def ttl(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _make_user(email: str, cognito_sub: str) -> User:
    user = User(email=email, cognito_sub=cognito_sub)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def owner_env(app, monkeypatch):
    """Seed an owner with one recipe and wire the auth stubs."""
    with app.app_context():
        owner = _make_user("owner@example.com", "sub-owner")
        owner_id = owner.id
        recipe = Recipe(
            user_id=owner_id,
            title="Garlic Chicken",
            created_at=datetime.datetime(2024, 1, 1, 12, 0, 0),
        )
        db.session.add(recipe)
        db.session.commit()

    monkeypatch.setattr(
        decorators, "validate_token", lambda token: {"sub": "sub-owner"}
    )
    monkeypatch.setattr(
        decorators,
        "resolve_current_user",
        lambda claims: User.query.get(owner_id),
    )
    return {"owner_id": owner_id}


def _stub_llm(monkeypatch):
    """Make the LLM seam return an empty ranking (so each call is cheap)."""
    monkeypatch.setattr(
        llm_search, "call_llm_rankings", lambda query, index: []
    )


def test_under_limit_requests_succeed(client, app, owner_env, monkeypatch):
    app.config["RATE_LIMIT_LLM_SEARCH_MAX"] = 3
    app.config["RATE_LIMIT_LLM_SEARCH_WINDOW_SECONDS"] = 60
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: _FakeRedis())
    _stub_llm(monkeypatch)

    for i in range(3):
        resp = client.post(
            "/v1/search/llm",
            json={"query": f"query {i}"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200


def test_over_limit_returns_429_with_retry_after(
    client, app, owner_env, monkeypatch
):
    app.config["RATE_LIMIT_LLM_SEARCH_MAX"] = 2
    app.config["RATE_LIMIT_LLM_SEARCH_WINDOW_SECONDS"] = 60
    # Shared fake so the counter persists across requests.
    monkeypatch.setattr(
        redis_client, "get_redis_client", lambda fake=_FakeRedis(): fake
    )
    _stub_llm(monkeypatch)

    for i in range(2):
        assert (
            client.post(
                "/v1/search/llm",
                json={"query": f"query {i}"},
                headers=_auth_headers(),
            ).status_code
            == 200
        )

    blocked = client.post(
        "/v1/search/llm",
        json={"query": "one too many"},
        headers=_auth_headers(),
    )
    assert blocked.status_code == 429
    assert "error" in blocked.get_json()
    assert "Retry-After" in blocked.headers
    assert int(blocked.headers["Retry-After"]) >= 1


def test_redis_outage_allows_request(client, app, owner_env, monkeypatch):
    app.config["RATE_LIMIT_LLM_SEARCH_MAX"] = 1
    app.config["RATE_LIMIT_LLM_SEARCH_WINDOW_SECONDS"] = 60
    monkeypatch.setattr(
        redis_client, "get_redis_client", lambda: _ExplodingRedis()
    )
    _stub_llm(monkeypatch)

    # Even well past the limit of 1, a Redis outage lets requests through.
    for i in range(3):
        resp = client.post(
            "/v1/search/llm",
            json={"query": f"query {i}"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
