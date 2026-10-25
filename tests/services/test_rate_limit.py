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
    """A non-repairable non-positive TTL is floored to 1.

    A ``-2`` TTL (the key vanished in the narrow gap before ``EXPIRE`` landed)
    is not self-healed — there is nothing to re-expire — so ``retry_after``
    falls back to the 1-second floor and is never 0/negative. Distinct from the
    ``-1`` lost-expiry case, which self-heals (see
    :func:`test_lost_ttl_self_heals_and_resets_window`).
    """
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fake)

    # Pretend INCR reports the caller is over the limit, but by the time we read
    # the TTL the key has gone (ttl() returns -2 for an unknown key).
    monkeypatch.setattr(fake, "incr", lambda key: 5)

    result = redis_client.check_rate_limit(
        "search_llm", "user-3", limit=1, window_seconds=60
    )
    assert result.allowed is False
    assert result.retry_after == 1


def test_lost_ttl_self_heals_and_resets_window(monkeypatch):
    """An over-limit key with TTL -1 (no expiry) is defensively re-expired.

    Guards against the lost-TTL wedge: if a counter ever loses its expiry while
    over the limit it would never reset and block the user indefinitely. The
    over-limit branch must observe the ``-1`` TTL, re-apply
    ``EXPIRE(key, window_seconds)`` so the window resets, and report
    ``retry_after == window_seconds``.
    """
    fake = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fake)

    # Seed an over-limit key that has lost its expiry (TTL -1, no expiry).
    key = "ratelimit:search_llm:user-wedged"
    fake.counts[key] = 5  # already over the small limit below
    fake.ttls[key] = -1

    result = redis_client.check_rate_limit(
        "search_llm", "user-wedged", limit=1, window_seconds=90
    )

    assert result.allowed is False
    # retry_after is the full window, not the 1-second floor.
    assert result.retry_after == 90
    # EXPIRE was re-applied so the window will actually reset.
    assert fake.ttls[key] == 90


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
