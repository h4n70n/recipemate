"""Unit tests for the Redis-backed rate limiter.

These exercise :func:`app.services.redis_client.check_rate_limit` directly,
without going through a route, so the fixed-window counter semantics and the
fail-open behaviour are pinned down in isolation:

* requests under the limit are allowed,
* the first request over the limit (the N+1th) is rejected with a
  ``retry_after`` of at least 1 second derived from the key's TTL,
* a Redis outage (every op raises :class:`redis.RedisError`) results in an
  ``allowed=True`` result — the limiter fails OPEN so a cache blip cannot lock
  users out.

Redis is replaced by an in-memory stub that implements only the surface the
limiter touches (``incr``/``expire``/``ttl``) and is wired in by monkeypatching
:func:`app.services.redis_client.get_redis_client`, mirroring the fake-redis
pattern in :mod:`tests.recipes.test_upload`.
"""

from __future__ import annotations

import redis

from app.services import redis_client


class _FakeRedis:
    """Minimal in-memory redis-py stand-in for the limiter.

    Implements ``incr`` (auto-creating the counter at 1), ``expire`` (records
    the TTL), and ``ttl`` (returns the recorded TTL, or ``-2`` when the key is
    unknown / ``-1`` when it has no expiry — matching redis-py's return codes).
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

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

    def incr(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")

    def expire(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")

    def ttl(self, *args, **kwargs):
        raise redis.ConnectionError("redis is down")


def test_under_limit_is_allowed(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fake)

    # limit=3: the first three calls are all allowed.
    for _ in range(3):
        result = redis_client.check_rate_limit(
            "search_llm", "user-1", limit=3, window_seconds=60
        )
        assert result.allowed is True
        assert result.retry_after == 0

    # EXPIRE was set once, on the first hit, for the whole window.
    assert fake.ttls["ratelimit:search_llm:user-1"] == 60


def test_over_limit_is_rejected_with_retry_after(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fake)

    # Exhaust the limit of 2.
    for _ in range(2):
        assert redis_client.check_rate_limit(
            "extract", "user-2", limit=2, window_seconds=45
        ).allowed is True

    # The N+1th call is rejected and carries a positive Retry-After from TTL.
    blocked = redis_client.check_rate_limit(
        "extract", "user-2", limit=2, window_seconds=45
    )
    assert blocked.allowed is False
    assert blocked.retry_after >= 1
    assert blocked.retry_after == 45


def test_retry_after_floored_at_one_second(monkeypatch):
    """A non-positive TTL is floored to 1 so Retry-After is never 0/negative."""
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fake)

    # Pretend the key exists but has lost its expiry (TTL -1).
    key = "ratelimit:search_llm:user-3"
    fake.counts[key] = 5  # already over any small limit
    fake.ttls[key] = -1

    result = redis_client.check_rate_limit(
        "search_llm", "user-3", limit=1, window_seconds=60
    )
    assert result.allowed is False
    assert result.retry_after == 1


def test_counters_are_scoped_per_endpoint_and_user(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fake)

    # Exhaust search_llm for user-a.
    for _ in range(2):
        redis_client.check_rate_limit("search_llm", "user-a", 2, 60)
    assert (
        redis_client.check_rate_limit("search_llm", "user-a", 2, 60).allowed
        is False
    )

    # A different scope for the same user is independent.
    assert (
        redis_client.check_rate_limit("extract", "user-a", 2, 60).allowed is True
    )
    # A different user on the same scope is independent.
    assert (
        redis_client.check_rate_limit("search_llm", "user-b", 2, 60).allowed
        is True
    )


def test_redis_outage_fails_open(monkeypatch):
    monkeypatch.setattr(
        redis_client, "get_redis_client", lambda: _ExplodingRedis()
    )

    result = redis_client.check_rate_limit(
        "search_llm", "user-x", limit=1, window_seconds=60
    )
    assert result.allowed is True
    assert result.retry_after == 0
