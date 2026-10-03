"""S3 helpers for direct-to-client uploads via presigned URLs.

This module is the write side of the asset flow: it mints short-lived
presigned URLs so clients upload bytes straight to S3 (and later read them
back) without the API ever proxying the payload. It is distinct from
:mod:`app.services.urls`, which builds *public, un-signed* asset URLs
(CloudFront / direct S3) for images that are already stored and world/CDN
readable. Presigned URLs here are for the pre-storage ``PUT`` and for signed
``GET`` access to objects that are not public.

boto3 client
------------
:func:`get_s3_client` builds a signature-v4 S3 client honouring the app's
``AWS_REGION`` and the optional ``AWS_ENDPOINT_URL``. The endpoint override is
what lets local development target LocalStack (e.g.
``AWS_ENDPOINT_URL=http://localhost:4566``) while deployed environments leave
it empty and hit real AWS. Signature v4 (``signature_version="s3v4"``) is
required for presigned ``PUT`` URLs that pin a ``Content-Type`` and for buckets
in regions that only accept v4.

Upload key scheme
-----------------
:func:`build_upload_key` returns ``uploads/<uuid4>/<basename>``:

* ``uploads/`` namespaces raw, not-yet-processed uploads apart from the
  processed ``images/``/``thumbnails/`` keys produced downstream.
* a fresh ``uuid4`` segment guarantees uniqueness and prevents one user's
  upload from colliding with or overwriting another's, even for identical
  filenames.
* the trailing segment is the *basename* of the client-supplied filename with
  any path separators stripped, so a hostile or sloppy ``filename`` like
  ``../../etc/passwd`` cannot escape the ``uploads/<uuid>/`` prefix.

The ``user_id`` is not encoded in the key — ownership is tracked out of band in
Redis (see :func:`app.services.redis_client.store_upload_metadata`) and on the
eventual recipe row. The key is intentionally opaque and unguessable via the
uuid segment.
"""

from __future__ import annotations

import os
import uuid

import boto3
from botocore.config import Config as BotoConfig
from flask import current_app


def get_s3_client():
    """Build a signature-v4 S3 client from the active app config.

    Honours ``AWS_REGION`` and, when set, ``AWS_ENDPOINT_URL`` (so local
    development can target LocalStack). The client uses
    ``signature_version="s3v4"``, which is required to presign ``PUT`` URLs
    that pin a ``Content-Type`` and to work against v4-only regions.

    Returns:
        A configured boto3 S3 client.
    """
    region = current_app.config.get("AWS_REGION") or "us-east-1"
    endpoint_url = current_app.config.get("AWS_ENDPOINT_URL") or None

    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        config=BotoConfig(signature_version="s3v4"),
    )


def _require_bucket() -> str:
    """Return the configured S3 bucket name or raise a clear error.

    Returns:
        The ``S3_BUCKET`` config value.

    Raises:
        RuntimeError: If no bucket is configured, so a missing/empty
            ``S3_BUCKET`` fails loudly here rather than producing a presigned
            URL pointing at an empty bucket name.
    """
    bucket = current_app.config.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not configured")
    return bucket


def build_upload_key(user_id: object, filename: str) -> str:
    """Build the S3 object key for a new upload: ``uploads/<uuid4>/<basename>``.

    The client-supplied ``filename`` is reduced to its basename (any directory
    components, with ``/`` *or* ``\\`` separators, are dropped) so a traversal
    attempt such as ``../../secret`` cannot escape the ``uploads/<uuid>/``
    prefix. A fresh ``uuid4`` segment guarantees the key is unique and
    unguessable regardless of how many clients upload the same filename.

    Args:
        user_id: The uploading user's id. Accepted for interface symmetry and
            future use; it is deliberately *not* encoded in the key (see the
            module docstring).
        filename: The client-declared filename. Only its basename is used.

    Returns:
        A key of the form ``uploads/<uuid4>/<sanitized-basename>``.
    """
    # Normalise both POSIX and Windows separators to a basename. ``os.path``
    # alone would miss ``\\`` on POSIX hosts, so split on both explicitly.
    candidate = str(filename).replace("\\", "/")
    basename = os.path.basename(candidate).strip()
    # A filename that was *only* separators/dots collapses to empty; fall back
    # to a safe placeholder so the key stays well-formed.
    if not basename or basename in (".", ".."):
        basename = "upload"

    return f"uploads/{uuid.uuid4()}/{basename}"


def generate_presigned_put(
    s3_key: str, content_type: str, expires_in: int = 900
) -> str:
    """Generate a presigned ``PUT`` URL for uploading an object to S3.

    The returned URL lets the client ``PUT`` bytes directly to
    ``s3_key`` in the configured bucket for ``expires_in`` seconds (default
    900 = 15 minutes, matching ``docs/api.md``). The ``ContentType`` is pinned
    into the signature, so the client must send a matching ``Content-Type``
    header on the upload.

    Args:
        s3_key: The destination object key (e.g. from :func:`build_upload_key`).
        content_type: The MIME type the upload will carry; pinned into the URL.
        expires_in: Lifetime of the URL in seconds. Defaults to 900 (15 min).

    Returns:
        The presigned ``https`` upload URL.
    """
    bucket = _require_bucket()
    client = get_s3_client()
    return client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": s3_key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )


def generate_presigned_get(s3_key: str, expires_in: int = 900) -> str:
    """Generate a presigned ``GET`` URL for reading a private S3 object.

    Use this for objects that are not publicly readable (so the un-signed URL
    builder in :mod:`app.services.urls` would not work). The URL is valid for
    ``expires_in`` seconds (default 900 = 15 minutes).

    Args:
        s3_key: The object key to read.
        expires_in: Lifetime of the URL in seconds. Defaults to 900 (15 min).

    Returns:
        The presigned ``https`` download URL.
    """
    bucket = _require_bucket()
    client = get_s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": s3_key},
        ExpiresIn=expires_in,
    )
