"""Tests for the Pillow-based thumbnail service (TASK-2.2).

Covers the three public pieces of :mod:`app.services.thumbnails`:

* :func:`generate_thumbnail` — the pure transform. Driven with in-memory
  images synthesised by Pillow (no fixtures on disk): asserts the result fits
  within the bounding box, decodes as a valid image, preserves aspect ratio,
  never upscales, and flattens RGBA transparency without crashing or going
  black.
* :func:`derive_thumbnail_key` — the deterministic ``uploads/`` →
  ``thumbnails/`` key mapping.
* :func:`create_and_store_thumbnail` — the S3 orchestration, exercised against
  :mod:`moto`'s in-process S3 (bucket created, a real image ``put`` at an
  uploads key) so no LocalStack/AWS is needed. Also asserts graceful ``None``
  on a non-image object and that nothing is raised.

S3 calls go through :func:`app.services.s3.get_s3_client`, which reads config
from the Flask app context, so the moto-backed tests run inside ``app_context``
provided by the :func:`app` fixture in :mod:`tests.conftest`.
"""

from __future__ import annotations

import io

import boto3
import pytest
from moto import mock_aws
from PIL import Image

from app.services import thumbnails

TEST_REGION = "us-east-1"
TEST_BUCKET = "recipemate-images"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_image_bytes(
    size: tuple[int, int], mode: str = "RGB", fmt: str = "PNG"
) -> bytes:
    """Synthesise an in-memory image of ``size`` and return its encoded bytes."""
    if mode == "RGBA":
        # A semi-transparent block so flattening has something to composite.
        image = Image.new("RGBA", size, (10, 120, 200, 128))
    else:
        image = Image.new(mode, size, (10, 120, 200))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


# ---------------------------------------------------------------------------
# generate_thumbnail — pure transform
# ---------------------------------------------------------------------------


def test_generate_thumbnail_fits_within_max_size():
    source = _make_image_bytes((1200, 800), fmt="PNG")

    out = thumbnails.generate_thumbnail(source, max_size=(400, 400))

    result = _open(out)
    assert result.width <= 400
    assert result.height <= 400
    # It actually shrank relative to the 1200x800 source.
    assert result.width < 1200
    assert result.height < 800


def test_generate_thumbnail_output_is_valid_jpeg():
    source = _make_image_bytes((1000, 1000), fmt="PNG")

    out = thumbnails.generate_thumbnail(source)

    result = _open(out)
    assert result.format == "JPEG"
    assert result.mode == "RGB"


def test_generate_thumbnail_preserves_aspect_ratio():
    # 1600x400 is a 4:1 landscape image.
    source = _make_image_bytes((1600, 400), fmt="PNG")

    out = thumbnails.generate_thumbnail(source, max_size=(400, 400))

    result = _open(out)
    source_ratio = 1600 / 400
    result_ratio = result.width / result.height
    # Aspect ratio preserved within rounding of integer pixel dimensions.
    assert result_ratio == pytest.approx(source_ratio, rel=0.05)
    # The long edge is the one that hit the 400 bound.
    assert result.width == 400
    assert result.height <= 400


def test_generate_thumbnail_does_not_upscale_small_image():
    # Smaller than the box: must come back unchanged in size (no upscaling).
    source = _make_image_bytes((120, 90), fmt="PNG")

    out = thumbnails.generate_thumbnail(source, max_size=(400, 400))

    result = _open(out)
    assert result.width == 120
    assert result.height == 90


def test_generate_thumbnail_flattens_rgba_without_crashing():
    source = _make_image_bytes((800, 600), mode="RGBA", fmt="PNG")

    out = thumbnails.generate_thumbnail(source, max_size=(400, 400))

    result = _open(out)
    # Flattened to RGB (JPEG has no alpha) and still within the box.
    assert result.mode == "RGB"
    assert result.width <= 400
    assert result.height <= 400


def test_generate_thumbnail_handles_jpeg_source():
    source = _make_image_bytes((900, 900), mode="RGB", fmt="JPEG")

    out = thumbnails.generate_thumbnail(source, max_size=(200, 200))

    result = _open(out)
    assert result.width <= 200
    assert result.height <= 200


# ---------------------------------------------------------------------------
# derive_thumbnail_key — deterministic mapping
# ---------------------------------------------------------------------------


def test_derive_thumbnail_key_maps_uploads_to_thumbnails():
    key = "uploads/1234-abcd/photo.png"
    assert (
        thumbnails.derive_thumbnail_key(key) == "thumbnails/1234-abcd/photo.jpg"
    )


def test_derive_thumbnail_key_is_deterministic():
    key = "uploads/abc/image.jpeg"
    first = thumbnails.derive_thumbnail_key(key)
    second = thumbnails.derive_thumbnail_key(key)
    assert first == second == "thumbnails/abc/image.jpg"


def test_derive_thumbnail_key_rewrites_extension_to_jpg():
    assert (
        thumbnails.derive_thumbnail_key("uploads/u/pic.HEIC")
        == "thumbnails/u/pic.jpg"
    )


def test_derive_thumbnail_key_handles_leading_slash():
    assert (
        thumbnails.derive_thumbnail_key("/uploads/u/pic.png")
        == "thumbnails/u/pic.jpg"
    )


def test_derive_thumbnail_key_non_uploads_prefix_still_under_thumbnails():
    key = "images/u/pic.png"
    out = thumbnails.derive_thumbnail_key(key)
    assert out.startswith("thumbnails/")
    assert out == "thumbnails/images/u/pic.jpg"


def test_derive_thumbnail_key_no_extension():
    assert (
        thumbnails.derive_thumbnail_key("uploads/u/noext")
        == "thumbnails/u/noext.jpg"
    )


# ---------------------------------------------------------------------------
# create_and_store_thumbnail — S3 orchestration (moto)
# ---------------------------------------------------------------------------


@pytest.fixture()
def s3_backend(app):
    """Start moto's S3 mock, create the bucket, and stay in the app context.

    ``create_and_store_thumbnail`` builds its client via
    :func:`app.services.s3.get_s3_client`, which reads ``S3_BUCKET`` /
    ``AWS_REGION`` from the Flask config, so the whole test body runs inside
    ``app.app_context()`` with moto intercepting the boto3 calls.
    """
    with app.app_context(), mock_aws():
        client = boto3.client("s3", region_name=TEST_REGION)
        client.create_bucket(Bucket=TEST_BUCKET)
        yield client


def test_create_and_store_thumbnail_stores_valid_smaller_image(s3_backend):
    image_key = "uploads/some-uuid/photo.png"
    source = _make_image_bytes((1500, 1000), fmt="PNG")
    s3_backend.put_object(Bucket=TEST_BUCKET, Key=image_key, Body=source)

    result_key = thumbnails.create_and_store_thumbnail(image_key)

    assert result_key == "thumbnails/some-uuid/photo.jpg"

    # The thumbnail object now exists and is a valid, smaller JPEG.
    stored = s3_backend.get_object(Bucket=TEST_BUCKET, Key=result_key)
    assert stored["ContentType"] == "image/jpeg"
    thumb_bytes = stored["Body"].read()
    thumb = _open(thumb_bytes)
    assert thumb.format == "JPEG"
    assert thumb.width <= 400
    assert thumb.height <= 400
    # Meaningfully smaller than the 1500x1000 original.
    assert thumb.width < 1500


def test_create_and_store_thumbnail_returns_none_on_non_image(s3_backend):
    image_key = "uploads/some-uuid/garbage.png"
    s3_backend.put_object(
        Bucket=TEST_BUCKET, Key=image_key, Body=b"not an image at all"
    )

    # Must not raise, and must report failure as None.
    result_key = thumbnails.create_and_store_thumbnail(image_key)
    assert result_key is None

    # No thumbnail object was written.
    with pytest.raises(s3_backend.exceptions.NoSuchKey):
        s3_backend.get_object(
            Bucket=TEST_BUCKET, Key="thumbnails/some-uuid/garbage.jpg"
        )


def test_create_and_store_thumbnail_returns_none_when_object_missing(s3_backend):
    # Nothing uploaded at this key; download fails and we degrade to None.
    result_key = thumbnails.create_and_store_thumbnail(
        "uploads/missing/none.png"
    )
    assert result_key is None
