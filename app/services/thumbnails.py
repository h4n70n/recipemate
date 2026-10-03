"""Thumbnail generation for uploaded recipe images (Pillow, in-process).

This is the processing side of the asset flow. After a client uploads a raw
image straight to S3 under an ``uploads/<uuid>/<basename>`` key (see
:mod:`app.services.s3`), a thumbnail needs to be produced and stored so recipe
cards and lists can show a small, bandwidth-cheap preview
(``Recipe.thumbnail_s3_key``, surfaced as a URL by :mod:`app.services.urls`).

Why Pillow-on-confirmation rather than a Lambda
------------------------------------------------
TASK-2.2 permits either a post-upload Lambda or in-process Pillow resizing.
This module takes the Pillow route deliberately: it keeps the whole operation
synchronous and in the API process, needs no extra deploy/IAM wiring, and —
most importantly — splits cleanly into a *pure* transform (:func:`generate_
thumbnail`, no I/O) and a thin S3 orchestration wrapper
(:func:`create_and_store_thumbnail`). That makes the resizing logic trivially
unit-testable and the S3 path exercisable with moto, with no running LocalStack
or Lambda runtime required.

Integration hook
----------------
There is no dedicated "confirm upload" endpoint in ``docs/api.md``; an uploaded
image becomes meaningful only when it is *attached to a recipe*. The async
extraction flow (TASK-2.3, ``POST /recipes/extract`` +
``lambda/extract_handler.py``) is where an ``uploads/...`` key is bound to a
recipe row via ``Recipe.image_s3_key``. :func:`create_and_store_thumbnail` is
the single function that flow (or any future manual attach/confirm path) calls
once it knows the image key: pass the image's S3 key, get back the thumbnail's
S3 key to persist in ``Recipe.thumbnail_s3_key`` (or ``None`` if the object
could not be turned into a thumbnail — see below). It is intentionally *not*
wired to a new public endpoint here, because inventing one would contradict the
documented API surface.

HEIC note
---------
iOS shares frequently produce HEIC images. Pillow cannot decode HEIC without
the optional ``pillow-heif`` plugin, so its import is attempted lazily and
guarded: if the plugin is installed it is registered and HEIC decodes
normally; if it is absent, a HEIC upload simply fails to open and is handled by
the same graceful degradation as any other non-decodable object
(:func:`create_and_store_thumbnail` logs a warning and returns ``None`` rather
than raising). ``pillow-heif`` is therefore an *optional* dependency, not a
hard requirement.
"""

from __future__ import annotations

import io
import logging

from flask import current_app
from PIL import Image, UnidentifiedImageError

from app.services.s3 import get_s3_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional HEIC support
# ---------------------------------------------------------------------------

# Attempt to register the HEIF/HEIC opener if ``pillow-heif`` is installed.
# This is best-effort and never fatal: without the plugin, HEIC images simply
# fail to decode and are handled by the graceful-degradation path below.
try:  # pragma: no cover - depends on an optional dependency being present
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    _HEIC_SUPPORTED = True
except Exception:  # noqa: BLE001 - any import/registration failure disables HEIC
    _HEIC_SUPPORTED = False


#: Default longest-edge bounding box for a thumbnail, in pixels. The image is
#: scaled to fit *within* this box preserving aspect ratio (never upscaled).
DEFAULT_MAX_SIZE: tuple[int, int] = (400, 400)

#: Default encoding for generated thumbnails. JPEG keeps thumbnails small and
#: is universally renderable; it has no alpha channel, which is why RGBA/P
#: inputs are flattened onto a white background before encoding.
DEFAULT_FORMAT: str = "JPEG"

#: JPEG quality for the encoded thumbnail — a sensible preview/size tradeoff.
_JPEG_QUALITY: int = 85


def generate_thumbnail(
    image_bytes: bytes,
    max_size: tuple[int, int] = DEFAULT_MAX_SIZE,
    fmt: str = DEFAULT_FORMAT,
) -> bytes:
    """Resize raw image bytes into a thumbnail and return the encoded bytes.

    Pure function: it performs no I/O and depends only on its arguments, so it
    is cheap to unit-test. The input is opened with Pillow, flattened to ``RGB``
    (any transparency — RGBA, LA, or palette images with alpha — is composited
    onto a white background rather than dropped to black), scaled to fit within
    ``max_size`` preserving aspect ratio (:meth:`PIL.Image.Image.thumbnail`
    never upscales a smaller image), and re-encoded as ``fmt``.

    Args:
        image_bytes: The raw bytes of the source image.
        max_size: ``(max_width, max_height)`` bounding box the result must fit
            within. Aspect ratio is preserved, so at most one dimension reaches
            the bound. Defaults to :data:`DEFAULT_MAX_SIZE` (400x400).
        fmt: The Pillow encoder/format name for the output (e.g. ``"JPEG"``,
            ``"PNG"``). Defaults to :data:`DEFAULT_FORMAT` (``"JPEG"``).

    Returns:
        The encoded thumbnail as ``bytes``.

    Raises:
        PIL.UnidentifiedImageError: If ``image_bytes`` is not a decodable
            image. (The S3 wrapper :func:`create_and_store_thumbnail` catches
            this and degrades gracefully; direct callers get the exception.)
    """
    with Image.open(io.BytesIO(image_bytes)) as image:
        # Force any lazy decode to happen now, inside the error-prone region.
        image.load()

        prepared = _flatten_to_rgb(image)

        # ``thumbnail`` resizes in place, preserves aspect ratio, and will not
        # enlarge an image already smaller than ``max_size``.
        prepared.thumbnail(max_size, Image.LANCZOS)

        buffer = io.BytesIO()
        save_kwargs: dict[str, object] = {}
        if fmt.upper() == "JPEG":
            save_kwargs["quality"] = _JPEG_QUALITY
            save_kwargs["optimize"] = True
        prepared.save(buffer, format=fmt, **save_kwargs)
        return buffer.getvalue()


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """Return an ``RGB`` copy of ``image``, compositing any alpha onto white.

    JPEG has no alpha channel, so a straight ``convert("RGB")`` on a
    transparent image turns transparent pixels black. Instead, images carrying
    transparency (``RGBA``/``LA``, or palette images with a transparency entry)
    are composited onto an opaque white canvas first so the thumbnail looks
    natural. Images without alpha are simply converted to ``RGB``.
    """
    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )

    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        # Use the alpha channel as the paste mask so transparent areas show
        # the white background rather than black.
        background.paste(rgba, mask=rgba.split()[-1])
        return background

    if image.mode == "RGB":
        return image.copy()
    return image.convert("RGB")


def derive_thumbnail_key(image_s3_key: str) -> str:
    """Map a raw-upload image key to its deterministic thumbnail key.

    The upload scheme (see :func:`app.services.s3.build_upload_key`) is
    ``uploads/<uuid>/<basename>``. The thumbnail for that image lives at the
    parallel ``thumbnails/`` prefix with the same remainder and a ``.jpg``
    extension (thumbnails are always JPEG — see :data:`DEFAULT_FORMAT`), e.g.::

        uploads/1234-.../photo.png  ->  thumbnails/1234-.../photo.jpg

    The mapping is a pure, deterministic string transform so the same image key
    always yields the same thumbnail key (callers can recompute it without
    storing it, and a re-run overwrites rather than duplicates). Only the first
    ``uploads/`` prefix is rewritten; a key that does not start with
    ``uploads/`` is still placed under ``thumbnails/`` so the result never
    collides with the source prefix.

    Args:
        image_s3_key: The source image's S3 object key.

    Returns:
        The derived thumbnail object key under ``thumbnails/`` ending in
        ``.jpg``.
    """
    key = image_s3_key.lstrip("/")

    if key.startswith("uploads/"):
        remainder = key[len("uploads/") :]
    else:
        remainder = key

    # Swap the extension to .jpg since thumbnails are always JPEG-encoded.
    dot = remainder.rfind(".")
    slash = remainder.rfind("/")
    if dot > slash:  # a real extension on the final path segment
        remainder = remainder[:dot]
    remainder = f"{remainder}.jpg"

    return f"thumbnails/{remainder}"


def create_and_store_thumbnail(image_s3_key: str) -> str | None:
    """Download an uploaded image, build a thumbnail, and store it in S3.

    This is the orchestration seam the attach/extraction flow calls once an
    uploaded image is bound to a recipe (see the module docstring's
    "Integration hook" section). It:

    1. downloads the object at ``image_s3_key`` from the configured bucket,
    2. runs :func:`generate_thumbnail` over the bytes,
    3. uploads the JPEG result to :func:`derive_thumbnail_key` with
       ``ContentType: image/jpeg``, and
    4. returns the thumbnail key for the caller to persist in
       ``Recipe.thumbnail_s3_key``.

    Graceful degradation: if the object cannot be fetched or is not a decodable
    image (a non-image upload, a truncated file, or an HEIC when
    ``pillow-heif`` is not installed), the error is caught, logged at warning
    level, and ``None`` is returned. Thumbnailing is a nice-to-have preview
    step and must never crash the caller that is attaching an image to a
    recipe — the recipe can still reference the full-size image.

    Args:
        image_s3_key: The S3 object key of the already-uploaded source image
            (typically an ``uploads/<uuid>/<basename>`` key).

    Returns:
        The thumbnail's S3 object key on success, or ``None`` if the thumbnail
        could not be produced or stored.
    """
    bucket = current_app.config.get("S3_BUCKET")
    if not bucket:
        logger.warning(
            "S3_BUCKET not configured; skipping thumbnail for %s", image_s3_key
        )
        return None

    client = get_s3_client()

    try:
        response = client.get_object(Bucket=bucket, Key=image_s3_key)
        image_bytes = response["Body"].read()
    except Exception as error:  # noqa: BLE001 - any download failure is non-fatal
        logger.warning(
            "Could not download %s for thumbnailing (skipping): %s",
            image_s3_key,
            error,
        )
        return None

    try:
        thumbnail_bytes = generate_thumbnail(image_bytes)
    except (UnidentifiedImageError, OSError, ValueError) as error:
        # Not a decodable image (random bytes, truncated file, unsupported
        # HEIC without the plugin, etc.). Degrade gracefully.
        logger.warning(
            "Could not generate thumbnail for %s (skipping): %s",
            image_s3_key,
            error,
        )
        return None

    thumbnail_key = derive_thumbnail_key(image_s3_key)

    try:
        client.put_object(
            Bucket=bucket,
            Key=thumbnail_key,
            Body=thumbnail_bytes,
            ContentType="image/jpeg",
        )
    except Exception as error:  # noqa: BLE001 - a store failure is non-fatal
        logger.warning(
            "Could not store thumbnail %s for %s (skipping): %s",
            thumbnail_key,
            image_s3_key,
            error,
        )
        return None

    return thumbnail_key
