"""Helpers for turning stored S3 object keys into public asset URLs.

Recipe images and thumbnails are stored in S3 and referenced in the database
only by their object key (e.g. ``thumbnails/<uuid>.jpg``). The public URL that
clients fetch is derived from that key at serialization time rather than being
persisted, so that moving the asset-serving layer (adding or changing a CDN)
never requires a data migration.

Resolution order for :func:`s3_key_to_url`:

1. If a CloudFront distribution domain is configured
   (``CLOUDFRONT_DOMAIN``), serve the asset from CloudFront:
   ``https://<cloudfront-domain>/<key>``. This is the preferred path in
   deployed environments — CloudFront fronts the bucket and provides caching
   and TLS on a stable domain.
2. Otherwise fall back to a direct virtual-hosted-style S3 URL built from the
   configured bucket and region:
   ``https://<bucket>.s3.<region>.amazonaws.com/<key>``. This keeps local and
   un-fronted environments working without a CDN.
3. If the key is ``None``/empty (e.g. a recipe without a thumbnail), return
   ``None`` so the serialized field is explicitly null.
"""

from __future__ import annotations

from flask import current_app


def s3_key_to_url(s3_key: str | None) -> str | None:
    """Build a public URL for an S3 object key.

    Args:
        s3_key: The stored S3 object key, or ``None`` when no asset exists.

    Returns:
        A public ``https`` URL for the object, or ``None`` when ``s3_key`` is
        falsy. A configured CloudFront domain is preferred; otherwise a direct
        S3 URL is returned.
    """
    if not s3_key:
        return None

    key = s3_key.lstrip("/")

    cloudfront_domain = current_app.config.get("CLOUDFRONT_DOMAIN")
    if cloudfront_domain:
        domain = cloudfront_domain.rstrip("/")
        return f"https://{domain}/{key}"

    bucket = current_app.config["S3_BUCKET"]
    region = current_app.config["AWS_REGION"]
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
