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

import redis
from flask import current_app

logger = logging.getLogger(__name__)

#: Prefix for upload-metadata keys: ``upload:<s3_key>``.
_KEY_PREFIX = "upload:"


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
