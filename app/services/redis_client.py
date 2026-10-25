"""Redis access for short-lived upload metadata.

After ``POST /recipes/upload`` mints a presigned URL, we stash a small record
describing the pending upload (who requested it, the original filename, the
declared content type) keyed by the S3 object key. A later step — the extract
flow or a thumbnail worker — can read this to attribute an uploaded object to a
user without trusting client-supplied data on the follow-up call. The record is
given a short TTL that matches the presigned URL's lifetime: once the upload
window closes, the metadata is irrelevant and expires on its own.

Best-effort by design
---------------------
Storing this metadata is **best-effort**. The presigned URL is the actual
product of the upload endpoint; the Redis record is a convenience. If Redis is
unreachable (connection refused, timeout, DNS failure) we log a warning and
return normally rather than failing the request — a transient cache outage must
not block a user from obtaining an upload URL. Callers therefore cannot assume
the metadata is present and should degrade gracefully when it is missing.

Client construction
-------------------
:func:`get_redis_client` builds a client lazily from the app's ``REDIS_URL`` on
each call (clients are cheap and connection-pooled by redis-py). It is written
as a single seam so tests can monkeypatch it with an in-memory stub, keeping
the suite from requiring a running Redis.
"""

from __future__ import annotations

import json
import logging
from typing import NamedTuple

import redis
from flask import current_app

logger = logging.getLogger(__name__)

#: Prefix for upload-metadata keys: ``upload:<s3_key>``.
_KEY_PREFIX = "upload:"

#: Prefix for per-user rate-limit counters: ``ratelimit:<scope>:<user_id>``.
#: Kept distinct from ``upload:`` so the two concerns never share a key space.
_RATE_LIMIT_PREFIX = "ratelimit:"


class RateLimitResult(NamedTuple):
    """Outcome of a :func:`check_rate_limit` call.

    Attributes:
        allowed: ``True`` if the request may proceed, ``False`` if the caller
            has exceeded the window's limit. Also ``True`` whenever Redis is
            unavailable (the limiter fails open — see :func:`check_rate_limit`).
        retry_after: Seconds the caller should wait before retrying, derived
            from the counter key's remaining TTL and floored at 1 so a
            ``Retry-After`` header is never 0 or negative. When the over-limit
            branch self-heals a lost TTL (see :func:`check_rate_limit`) this is
            the full ``window_seconds``. ``0`` when the request was allowed.
    """

    allowed: bool
    retry_after: int


def _rate_limit_key(scope: str, user_id: object) -> str:
    """Return the namespaced Redis key for a per-user rate-limit counter.

    ``scope`` names the limited operation (e.g. ``"search_llm"`` or
    ``"extract"``) so each endpoint's counter is independent; ``user_id`` is
    stringified so a UUID serialises cleanly.
    """
    return f"{_RATE_LIMIT_PREFIX}{scope}:{user_id}"


def get_redis_client():
    """Build a Redis client from the active app's ``REDIS_URL``.

    Constructed per call (redis-py pools connections internally, so this is
    cheap) and kept as a single, easily monkeypatched seam for tests.
    ``decode_responses=True`` so reads come back as ``str`` rather than
    ``bytes``.

    Returns:
        A :class:`redis.Redis` client.
    """
    url = current_app.config["REDIS_URL"]
    return redis.Redis.from_url(url, decode_responses=True)


def _upload_key(s3_key: str) -> str:
    """Return the namespaced Redis key for an upload's metadata."""
    return f"{_KEY_PREFIX}{s3_key}"


def store_upload_metadata(
    s3_key: str,
    user_id: object,
    filename: str,
    content_type: str,
    ttl: int = 900,
) -> bool:
    """Best-effort store of pending-upload metadata under ``upload:<s3_key>``.

    Writes a JSON document ``{"user_id", "filename", "content_type"}`` with an
    expiry of ``ttl`` seconds (default 900 = 15 min, matching the presigned
    URL's lifetime). ``user_id`` is stringified so a UUID serialises cleanly.

    This is intentionally non-fatal: any Redis error is caught, logged at
    warning level, and swallowed so the caller (the upload endpoint) can still
    return the presigned URL. The boolean return lets callers/tests observe
    whether the write actually happened.

    Args:
        s3_key: The object key the upload targets; forms the Redis key.
        user_id: The requesting user's id (stringified before storage).
        filename: The original client-declared filename.
        content_type: The declared MIME type of the upload.
        ttl: Expiry in seconds. Defaults to 900 (15 min).

    Returns:
        ``True`` if the metadata was stored, ``False`` if Redis was
        unavailable and the write was skipped.
    """
    payload = json.dumps(
        {
            "user_id": str(user_id),
            "filename": filename,
            "content_type": content_type,
        }
    )

    try:
        client = get_redis_client()
        client.set(_upload_key(s3_key), payload, ex=ttl)
        return True
    except redis.RedisError as error:
        # Transient cache outage must not block issuing an upload URL.
        logger.warning(
            "Failed to store upload metadata for %s (continuing): %s",
            s3_key,
            error,
        )
        return False


def check_rate_limit(
    scope: str,
    user_id: object,
    limit: int,
    window_seconds: int,
) -> RateLimitResult:
    """Apply a per-user fixed-window rate limit and report the outcome.

    Implements a classic fixed-window counter keyed by
    ``ratelimit:<scope>:<user_id>``:

    * ``INCR`` the counter (which creates it at ``1`` on the first request of a
      new window), then on that first hit (``count == 1``) set ``EXPIRE
      window_seconds`` so the whole window — and therefore the counter — resets
      after ``window_seconds``.
    * The request is ``allowed`` while ``count <= limit``.
    * When over the limit, ``retry_after`` is derived from the key's remaining
      ``TTL`` and floored at 1 second, so a ``Retry-After`` header built from it
      is never 0 or negative (a just-created key can momentarily report a TTL of
      ``-1``/``-2`` between the ``INCR`` and ``EXPIRE``).

    Self-healing lost TTLs
    ----------------------
    ``EXPIRE`` is normally issued only on the first hit of a window
    (``count == 1``). If a counter key ever loses its expiry — e.g. an
    ``EXPIRE`` that silently failed to land while ``INCR`` succeeded, or an
    operational ``PERSIST`` — a fixed-window counter would never reset and the
    user would stay wedged over the limit indefinitely while Redis is healthy.
    To defend against that, whenever the over-limit branch observes a
    **non-positive TTL** (``-1`` no-expiry) on the key, this re-applies
    ``EXPIRE(key, window_seconds)`` so the window actually resets, and uses
    ``window_seconds`` as the ``retry_after``. Any other non-positive/edge TTL
    that is not repaired (e.g. ``-2`` for a key that vanished between ``INCR``
    and the ``TTL`` read) still falls back to the 1-second floor.

    Window semantics: the limit is ``limit`` requests per fixed ``window_seconds``
    window that begins at the first request. All requests in that window share
    one counter; the allowance refills in full when the window expires (not a
    sliding/token-bucket refill).

    Fail-open policy
    ----------------
    This limiter is **best-effort** and deliberately **fails open**: any
    :class:`redis.RedisError` (connection refused, timeout, DNS failure) is
    caught, logged at warning level, and results in an ``allowed=True`` result.
    A transient cache blip must never lock legitimate users out of these
    endpoints. The security trade-off is that while Redis is down the
    application-level per-user limit is not enforced; the edge **AWS WAF
    rate-based rule** (added by the OR-5 infra work) is the backstop that still
    throttles abusive traffic in that window. Constructs its client only via
    :func:`get_redis_client` so tests can monkeypatch the seam.

    Args:
        scope: Names the limited operation; keeps each endpoint's counter
            independent (e.g. ``"search_llm"``, ``"extract"``).
        user_id: The authenticated caller's id (stringified into the key).
        limit: Maximum allowed requests per window.
        window_seconds: Length of the fixed window in seconds.

    Returns:
        A :class:`RateLimitResult` with ``allowed`` and ``retry_after``.
    """
    key = _rate_limit_key(scope, user_id)

    try:
        client = get_redis_client()
        count = client.incr(key)
        if count == 1:
            # First request in a new window — start the expiry clock.
            client.expire(key, window_seconds)

        if count <= limit:
            return RateLimitResult(allowed=True, retry_after=0)

        # Over the limit: tell the caller when the window resets.
        ttl = client.ttl(key)
        if isinstance(ttl, int) and ttl > 0:
            retry_after = ttl
        elif ttl == -1:
            # The key exists but lost its expiry (TTL -1): the counter would
            # never reset and wedge the user. Self-heal by re-applying EXPIRE
            # so the window resets, and report the full window as retry_after.
            client.expire(key, window_seconds)
            retry_after = window_seconds
        else:
            # Any other non-positive/edge TTL (e.g. -2 for a key that vanished
            # in the narrow gap before EXPIRE landed): floor at 1 second so a
            # Retry-After header is never 0 or negative.
            retry_after = 1
        return RateLimitResult(allowed=False, retry_after=retry_after)
    except redis.RedisError as error:
        # Fail open: a cache outage must not block legitimate users. The edge
        # WAF rate-based rule is the backstop while Redis is unavailable.
        logger.warning(
            "Rate-limit check failed for %s (allowing request): %s",
            key,
            error,
        )
        return RateLimitResult(allowed=True, retry_after=0)
