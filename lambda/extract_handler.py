"""
Recipe extraction Lambda handler.

Triggered by SQS messages enqueued when a user requests extraction of an
uploaded image.  Each message references an S3 key and a recipe id.  The
handler downloads the image from S3, calls the GPT-4o vision API with a
structured extraction prompt, parses the structured JSON response, and
updates the recipe record (plus its ingredient/tool/instruction child rows)
in PostgreSQL, then publishes an SNS notification so a push-notification
path can fan the result out to the user's device.

Environment variables
----------------------
  - OPENAI_SECRET_NAME          : Secrets Manager name holding the OpenAI API key.
  - OPENAI_SECRET_JSON_KEY      : JSON key within that secret (default OPENAI_API_KEY).
  - S3_BUCKET                   : Name of the bucket holding uploaded recipe images.
  - DATABASE_URL                : PostgreSQL connection string.
  - SNS_NOTIFICATIONS_TOPIC_ARN : (optional) SNS topic to publish completion to.

The OpenAI key is fetched from Secrets Manager at runtime (and cached for the
lifetime of the warm container) rather than injected as a plaintext env var, so
the key never appears in the function's configuration or the deploy template.

Design decisions
================

Database access — SQLAlchemy Core, no Flask
-------------------------------------------
This Lambda runs *outside* the Flask application. The ORM models in
``app/models`` all do ``from app import db`` (the Flask-SQLAlchemy instance),
so importing them would drag in Flask and an application context the Lambda
runtime does not have. To stay fully decoupled we therefore do **not** import
``app.models``. Instead we build a plain SQLAlchemy ``Engine`` from
``DATABASE_URL`` (cached per warm container) and issue parameterised
``UPDATE``/``DELETE``/``INSERT`` statements with :func:`sqlalchemy.text`
against the known table and column names documented in ``docs/data-model.md``.
Parameters are always bound (never string-interpolated) so there is no SQL
injection surface even though the model values originate from an LLM.

Thumbnailing — self-contained, inline, best-effort
---------------------------------------------------
``app.services.thumbnails.create_and_store_thumbnail`` is Flask-bound (it reads
``current_app.config`` and uses ``app.services.s3.get_s3_client``), so it
cannot be reused here either. We instead generate the thumbnail inline with
Pillow from the image bytes we already downloaded and upload it to a
``thumbnails/<...>.jpg`` key, mirroring the key-derivation and encoding rules
of the in-process service. This step is strictly best-effort: any failure is
logged and swallowed, and extraction still completes. We never fail an
extraction over a missing thumbnail.

Retry policy — transient raises, bad input marks failed
-------------------------------------------------------
SQS redelivers (and eventually dead-letters, after ``maxReceiveCount``) a
message only when the invocation *raises*. We use that deliberately:

  * **Transient failures** — S3 download errors, OpenAI call errors, DB
    connectivity errors, or an unparseable/garbage LLM response — are
    re-raised so SQS retries and, after repeated failure, routes the message
    to the DLQ. The recipe's ``extraction_status`` is first flipped to
    ``'failed'`` in its own committed transaction so the row reflects reality
    between attempts.
  * **Definitively bad input** — the referenced ``recipe_id`` does not exist,
    or the S3 object is not a decodable image — is *not* retried: retrying
    cannot help. We mark the recipe ``'failed'`` (when the row exists) and
    return normally so the message is deleted from the queue.

Testability — lazy clients, small seams
----------------------------------------
boto3/openai clients and the SQLAlchemy engine are constructed lazily inside
functions (never at import time), so the module imports cleanly with no AWS
credentials and no network. The externally-effecting steps are factored into
small single-purpose helpers — :func:`_download_image`, :func:`_call_gpt4o`,
:func:`_parse_extraction`, :func:`_persist_extraction`, :func:`_set_status`,
:func:`_maybe_store_thumbnail`, and :func:`_publish_notification` — so a test
can monkeypatch each seam independently without touching AWS or a real LLM.
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import uuid
from functools import lru_cache
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Exceptions used to drive the retry policy
# ---------------------------------------------------------------------------


class BadInputError(Exception):
    """A definitively bad input that must NOT be retried.

    Raised internally for conditions where a retry cannot possibly succeed
    (the recipe row does not exist, the S3 object is not a decodable image).
    The handler marks the recipe ``'failed'`` where possible and returns
    normally so SQS deletes the message instead of redelivering it.
    """


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_openai_api_key() -> str:
    """Fetch and cache the OpenAI API key from Secrets Manager.

    Resolves the secret named by ``OPENAI_SECRET_NAME`` and returns the value
    under ``OPENAI_SECRET_JSON_KEY`` (default ``OPENAI_API_KEY``). Secrets are
    stored as a small JSON document; a raw-string secret is also tolerated.

    The result is cached for the life of the warm Lambda container so repeated
    invocations do not re-hit Secrets Manager on every message.
    """
    secret_name = os.environ["OPENAI_SECRET_NAME"]
    json_key = os.environ.get("OPENAI_SECRET_JSON_KEY", "OPENAI_API_KEY")

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    raw = response.get("SecretString")
    if raw is None:
        raise RuntimeError(f"Secret {secret_name} has no string value")

    try:
        return json.loads(raw)[json_key]
    except (json.JSONDecodeError, KeyError, TypeError):
        # Secret stored as a bare string rather than JSON — use it directly.
        return raw


# ---------------------------------------------------------------------------
# Lazy clients / engine (never built at import time)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_engine():
    """Build and cache a SQLAlchemy engine from ``DATABASE_URL``.

    Lazily imported and constructed so the module imports with no DB driver /
    no connection, and so a warm container reuses one pooled engine across
    invocations. ``pool_pre_ping`` guards against stale connections that a
    long-idle Lambda container may be holding.
    """
    from sqlalchemy import create_engine

    database_url = os.environ["DATABASE_URL"]
    return create_engine(database_url, pool_pre_ping=True, future=True)


def _get_s3_client():
    """Return a boto3 S3 client (constructed lazily, per call)."""
    return boto3.client("s3")


def _get_sns_client():
    """Return a boto3 SNS client (constructed lazily, per call)."""
    return boto3.client("sns")


def _get_openai_client():
    """Return an OpenAI client authenticated with the Secrets Manager key.

    Imported lazily so the module does not require the ``openai`` package to be
    importable merely to be loaded, and so no network/credential work happens
    at import time.
    """
    from openai import OpenAI

    return OpenAI(api_key=get_openai_api_key())


# ---------------------------------------------------------------------------
# Prompt design (the "structured JSON extraction prompt" deliverable)
# ---------------------------------------------------------------------------

#: Vision model to use. GPT-4o accepts image inputs and JSON-mode responses.
MODEL = "gpt-4o"

#: System prompt instructing the model to act as a careful recipe extractor and
#: to emit STRICT JSON only. The exact key/type contract below is what
#: :func:`_parse_extraction` validates and coerces.
EXTRACTION_SYSTEM_PROMPT = """\
You are a meticulous recipe extraction assistant. You are given a single image
that contains a cooking recipe (a screenshot, a photo of a cookbook page, or a
social post). Read everything legible in the image and extract the recipe into
a single STRICT JSON object. Respond with JSON ONLY — no prose, no markdown
code fences, no commentary.

The JSON object MUST use exactly these keys:

{
  "title": string,                         // recipe name; "" if not legible
  "description": string | null,            // one-sentence summary, or null
  "prep_time_min": integer | null,         // preparation time in whole minutes
  "cook_time_min": integer | null,         // cooking time in whole minutes
  "total_time_min": integer | null,        // total time in whole minutes
  "servings": integer | null,              // number of servings/yield
  "ingredients": [                         // ordered as they appear
    {
      "name": string,                      // ingredient name, e.g. "flour"
      "quantity": number | null,           // numeric amount, e.g. 1.5; null if none
      "unit": string | null,               // e.g. "cup", "g", "tbsp"; null if none
      "preparation": string | null         // e.g. "finely chopped"; null if none
    }
  ],
  "tools": [string],                       // equipment, e.g. ["oven", "whisk"]
  "instructions": [string]                 // ordered steps, one per array entry
}

Rules:
- Convert fractions and ranges to a single decimal number where reasonable
  (e.g. "1 1/2" -> 1.5; for a range like "2-3" pick the lower bound).
- Times must be whole minutes. Convert hours to minutes (e.g. "1 hr" -> 60).
- Omit nothing structural: always include every key. Use null (for scalars),
  "" (for an unknown title), or [] (for lists) when information is absent.
- Do NOT invent ingredients, tools, or steps that are not supported by the
  image. Partial extraction is expected and acceptable.
"""

#: The user-turn text that accompanies the image in the chat completion.
EXTRACTION_USER_PROMPT = (
    "Extract the recipe from this image into the strict JSON object described. "
    "Return JSON only."
)


# ---------------------------------------------------------------------------
# S3 download
# ---------------------------------------------------------------------------


def _download_image(bucket: str, s3_key: str) -> bytes:
    """Download the raw image bytes for ``s3_key`` from ``bucket``.

    Any failure (missing object, access error, network) propagates to the
    caller; the handler treats a download failure as transient and re-raises
    so SQS retries.
    """
    client = _get_s3_client()
    response = client.get_object(Bucket=bucket, Key=s3_key)
    return response["Body"].read()


# ---------------------------------------------------------------------------
# GPT-4o call
# ---------------------------------------------------------------------------


def _guess_mime_type(image_bytes: bytes) -> str:
    """Best-effort MIME sniff from magic bytes for the data-URI prefix.

    GPT-4o only needs a plausible image content type on the data URI; the
    exact subtype is not critical. Falls back to ``image/jpeg``.
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _call_gpt4o(image_bytes: bytes) -> str:
    """Call GPT-4o vision with the extraction prompt and return the raw text.

    The image is sent as a base64 data URI alongside the structured-JSON
    instructions. JSON mode (``response_format={"type": "json_object"}``) is
    requested so the model returns a single JSON object. Returns the raw string
    content of the first choice; parsing/validation happens in
    :func:`_parse_extraction`.
    """
    mime = _guess_mime_type(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"

    client = _get_openai_client()
    completion = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": EXTRACTION_USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
        ],
    )
    return completion.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Parse & validate (partial-tolerant)
# ---------------------------------------------------------------------------

#: Fallback title when the model returns a missing/blank one. Mirrors the
#: producer's placeholder (``app/routes/recipes.py``) so the NOT NULL
#: ``recipes.title`` always holds.
DEFAULT_TITLE = "Untitled recipe"


def _coerce_int(value: Any) -> int | None:
    """Coerce ``value`` to an int, or return ``None`` if it is not numeric.

    Accepts ints, floats, and numeric strings (e.g. ``"45"``, ``"45 min"`` ->
    ``45``). Anything else becomes ``None`` so a malformed time/serving value
    never breaks the whole extraction.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        # Pull the leading numeric token out of e.g. "45 minutes".
        token = ""
        for ch in stripped:
            if ch.isdigit() or (ch == "-" and not token):
                token += ch
            else:
                break
        if token and token != "-":
            try:
                return int(token)
            except ValueError:
                return None
    return None


def _coerce_float(value: Any) -> float | None:
    """Coerce ``value`` to a float, or ``None`` if not numeric.

    Used for ingredient quantities (``recipes`` schema stores NUMERIC). A
    non-numeric quantity (e.g. "a pinch") becomes ``None`` rather than failing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce_str(value: Any) -> str | None:
    """Return a non-empty stripped string, or ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    stripped = value.strip()
    return stripped or None


def _parse_extraction(raw: str) -> dict[str, Any]:
    """Parse and defensively validate the model's JSON into a clean dict.

    Partial extractions are handled gracefully:
      * A missing/blank ``title`` falls back to :data:`DEFAULT_TITLE` so the
        NOT NULL column holds.
      * Scalar time/serving fields that are missing or non-numeric become
        ``None``.
      * Each ingredient must have a usable name to survive; a non-numeric
        quantity becomes ``None`` and malformed entries are dropped.
      * Tools and instructions that are blank/malformed are dropped rather than
        failing the whole job; the survivors keep their order.

    Raises:
        BadInputError: never (parsing a value is partial-tolerant).
        ValueError: if ``raw`` is not a JSON object at all — the handler treats
            this as a transient/garbage response and re-raises for SQS retry.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError(f"LLM response was not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise ValueError("LLM response JSON was not an object")

    title = _coerce_str(data.get("title")) or DEFAULT_TITLE

    # Ingredients: keep order, require a name, coerce quantity defensively.
    ingredients: list[dict[str, Any]] = []
    raw_ingredients = data.get("ingredients")
    if isinstance(raw_ingredients, list):
        for item in raw_ingredients:
            if not isinstance(item, dict):
                continue
            name = _coerce_str(item.get("name"))
            if not name:
                continue  # drop nameless ingredient entries
            ingredients.append(
                {
                    "name": name,
                    "quantity": _coerce_float(item.get("quantity")),
                    "unit": _coerce_str(item.get("unit")),
                    "preparation": _coerce_str(item.get("preparation")),
                }
            )

    # Tools: list of non-empty strings, order preserved.
    tools: list[str] = []
    raw_tools = data.get("tools")
    if isinstance(raw_tools, list):
        for item in raw_tools:
            name = _coerce_str(item)
            if name:
                tools.append(name)

    # Instructions: list of non-empty strings, order preserved.
    instructions: list[str] = []
    raw_instructions = data.get("instructions")
    if isinstance(raw_instructions, list):
        for item in raw_instructions:
            body = _coerce_str(item)
            if body:
                instructions.append(body)

    return {
        "title": title,
        "description": _coerce_str(data.get("description")),
        "prep_time_min": _coerce_int(data.get("prep_time_min")),
        "cook_time_min": _coerce_int(data.get("cook_time_min")),
        "total_time_min": _coerce_int(data.get("total_time_min")),
        "servings": _coerce_int(data.get("servings")),
        "ingredients": ingredients,
        "tools": tools,
        "instructions": instructions,
    }


# ---------------------------------------------------------------------------
# Persistence (SQLAlchemy Core, parameterised)
# ---------------------------------------------------------------------------


def _set_status(recipe_id: str, status: str) -> None:
    """Set ``recipes.extraction_status`` for ``recipe_id`` in its own commit.

    Used to flip the row to ``'processing'`` at the start and to ``'failed'``
    on a hard error, each in a short self-contained transaction so the status
    reflects reality independently of the main persistence transaction.
    """
    from sqlalchemy import text

    engine = _get_engine()
    now = datetime.datetime.utcnow()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE recipes "
                "SET extraction_status = :status, updated_at = :now "
                "WHERE id = :recipe_id"
            ),
            {"status": status, "now": now, "recipe_id": recipe_id},
        )


def _recipe_exists(recipe_id: str) -> bool:
    """Return whether a ``recipes`` row with ``recipe_id`` exists."""
    from sqlalchemy import text

    engine = _get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM recipes WHERE id = :recipe_id"),
            {"recipe_id": recipe_id},
        ).first()
    return row is not None


def _persist_extraction(
    recipe_id: str, extracted: dict[str, Any], thumbnail_s3_key: str | None
) -> None:
    """Persist the extracted recipe + children in a single transaction.

    Updates the recipe's scalar fields, deletes any existing
    ingredient/tool/instruction rows for the recipe (so a re-run is
    idempotent), inserts the freshly extracted children with correct
    ``sort_order``/``step_number``, sets ``extraction_status='complete'`` and
    bumps ``updated_at``. All statements are parameterised.

    ``thumbnail_s3_key`` is written only when non-None so a failed best-effort
    thumbnail does not clobber an existing value with NULL.
    """
    from sqlalchemy import text

    engine = _get_engine()
    now = datetime.datetime.utcnow()

    with engine.begin() as conn:
        # 1) Update recipe scalar fields + status.
        set_thumbnail = ""
        params: dict[str, Any] = {
            "title": extracted["title"],
            "description": extracted["description"],
            "prep_time_min": extracted["prep_time_min"],
            "cook_time_min": extracted["cook_time_min"],
            "total_time_min": extracted["total_time_min"],
            "servings": extracted["servings"],
            "now": now,
            "recipe_id": recipe_id,
        }
        if thumbnail_s3_key is not None:
            set_thumbnail = "thumbnail_s3_key = :thumbnail_s3_key, "
            params["thumbnail_s3_key"] = thumbnail_s3_key

        conn.execute(
            text(
                "UPDATE recipes SET "
                "title = :title, "
                "description = :description, "
                "prep_time_min = :prep_time_min, "
                "cook_time_min = :cook_time_min, "
                "total_time_min = :total_time_min, "
                "servings = :servings, "
                f"{set_thumbnail}"
                "extraction_status = 'complete', "
                "updated_at = :now "
                "WHERE id = :recipe_id"
            ),
            params,
        )

        # 2) Idempotency: clear existing children for this recipe.
        for table in ("ingredients", "tools", "instructions"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE recipe_id = :recipe_id"),
                {"recipe_id": recipe_id},
            )

        # 3) Insert ingredients with sort_order.
        for sort_order, ing in enumerate(extracted["ingredients"]):
            conn.execute(
                text(
                    "INSERT INTO ingredients "
                    "(id, recipe_id, name, quantity, unit, preparation, sort_order) "
                    "VALUES "
                    "(:id, :recipe_id, :name, :quantity, :unit, :preparation, :sort_order)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "recipe_id": recipe_id,
                    "name": ing["name"],
                    "quantity": ing["quantity"],
                    "unit": ing["unit"],
                    "preparation": ing["preparation"],
                    "sort_order": sort_order,
                },
            )

        # 4) Insert tools with sort_order.
        for sort_order, tool_name in enumerate(extracted["tools"]):
            conn.execute(
                text(
                    "INSERT INTO tools (id, recipe_id, name, sort_order) "
                    "VALUES (:id, :recipe_id, :name, :sort_order)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "recipe_id": recipe_id,
                    "name": tool_name,
                    "sort_order": sort_order,
                },
            )

        # 5) Insert instructions with step_number starting at 1.
        for index, body in enumerate(extracted["instructions"]):
            conn.execute(
                text(
                    "INSERT INTO instructions (id, recipe_id, step_number, body) "
                    "VALUES (:id, :recipe_id, :step_number, :body)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "recipe_id": recipe_id,
                    "step_number": index + 1,
                    "body": body,
                },
            )


# ---------------------------------------------------------------------------
# Thumbnail (best-effort, inline, self-contained)
# ---------------------------------------------------------------------------


def _derive_thumbnail_key(image_s3_key: str) -> str:
    """Map a raw-upload image key to its deterministic thumbnail key.

    Mirrors ``app.services.thumbnails.derive_thumbnail_key``: an
    ``uploads/<...>`` key is rewritten under ``thumbnails/`` with a ``.jpg``
    extension; any other key is still placed under ``thumbnails/`` so the
    result never collides with the source prefix.
    """
    key = image_s3_key.lstrip("/")
    if key.startswith("uploads/"):
        remainder = key[len("uploads/") :]
    else:
        remainder = key

    dot = remainder.rfind(".")
    slash = remainder.rfind("/")
    if dot > slash:
        remainder = remainder[:dot]
    return f"thumbnails/{remainder}.jpg"


def _maybe_store_thumbnail(
    bucket: str, image_s3_key: str, image_bytes: bytes
) -> str | None:
    """Generate a JPEG thumbnail inline and upload it; best-effort.

    Returns the thumbnail S3 key on success, or ``None`` if the image could not
    be decoded or the upload failed. Never raises: thumbnailing is a
    nice-to-have and must not fail an otherwise-successful extraction.
    """
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                prepared = background
            elif image.mode == "RGB":
                prepared = image.copy()
            else:
                prepared = image.convert("RGB")

            prepared.thumbnail((400, 400), Image.LANCZOS)
            buffer = io.BytesIO()
            prepared.save(buffer, format="JPEG", quality=85, optimize=True)
            thumbnail_bytes = buffer.getvalue()
    except Exception as error:  # noqa: BLE001 - thumbnail is best-effort
        logger.warning(
            "Could not generate thumbnail for %s (skipping): %s",
            image_s3_key,
            error,
        )
        return None

    thumbnail_key = _derive_thumbnail_key(image_s3_key)
    try:
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=thumbnail_key,
            Body=thumbnail_bytes,
            ContentType="image/jpeg",
        )
    except Exception as error:  # noqa: BLE001 - store failure is non-fatal
        logger.warning(
            "Could not store thumbnail %s for %s (skipping): %s",
            thumbnail_key,
            image_s3_key,
            error,
        )
        return None

    return thumbnail_key


# ---------------------------------------------------------------------------
# SNS notification (best-effort)
# ---------------------------------------------------------------------------


def _publish_notification(
    recipe_id: str, user_id: str | None, status: str, title: str
) -> None:
    """Publish a completion notification to SNS if a topic is configured.

    Best-effort: if ``SNS_NOTIFICATIONS_TOPIC_ARN`` is unset the step is a
    no-op, and any publish failure is logged and swallowed so it never fails an
    otherwise-successful extraction. The message body is JSON so the
    push-notification consumer (TASK-5.1) can route it to the user's device.
    """
    topic_arn = os.environ.get("SNS_NOTIFICATIONS_TOPIC_ARN")
    if not topic_arn:
        logger.info("SNS_NOTIFICATIONS_TOPIC_ARN not set; skipping notification")
        return

    message = json.dumps(
        {
            "recipe_id": recipe_id,
            "user_id": user_id,
            "status": status,
            "title": title,
        }
    )
    try:
        _get_sns_client().publish(
            TopicArn=topic_arn,
            Message=message,
            Subject="Recipe extraction complete",
        )
    except Exception as error:  # noqa: BLE001 - notification is best-effort
        logger.warning(
            "Could not publish SNS notification for recipe %s (skipping): %s",
            recipe_id,
            error,
        )


# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------


def _process_record(payload: dict[str, Any]) -> None:
    """Run the full extraction pipeline for one decoded SQS message body.

    See the module docstring's "Retry policy" section for which failures raise
    (transient — SQS retries) versus which are swallowed after marking the
    recipe ``'failed'`` (bad input — no retry).
    """
    recipe_id = payload.get("recipe_id")
    s3_key = payload.get("s3_key")
    user_id = payload.get("user_id")

    if not recipe_id or not s3_key:
        # Structurally invalid message — retrying cannot help.
        raise BadInputError(
            f"message missing recipe_id/s3_key: {payload!r}"
        ) from None

    bucket = os.environ["S3_BUCKET"]

    # Bad input: the recipe row does not exist. Nothing to mark; do not retry.
    if not _recipe_exists(recipe_id):
        logger.error("Recipe %s does not exist; dropping message", recipe_id)
        return

    # Mark processing up front so the poll endpoint reflects in-flight work.
    _set_status(recipe_id, "processing")

    try:
        image_bytes = _download_image(bucket, s3_key)
        raw = _call_gpt4o(image_bytes)
        extracted = _parse_extraction(raw)
    except BadInputError:
        # Definitively bad input — mark failed, do not retry.
        _set_status(recipe_id, "failed")
        logger.exception("Bad input for recipe %s; marking failed", recipe_id)
        return
    except Exception:
        # Transient (S3 / OpenAI / unparseable response). Mark failed so the
        # row reflects reality, then re-raise so SQS retries / dead-letters.
        _set_status(recipe_id, "failed")
        logger.exception(
            "Transient failure extracting recipe %s; re-raising for retry",
            recipe_id,
        )
        raise

    # Best-effort thumbnail from the bytes we already have.
    thumbnail_s3_key = _maybe_store_thumbnail(bucket, s3_key, image_bytes)

    try:
        _persist_extraction(recipe_id, extracted, thumbnail_s3_key)
    except Exception:
        _set_status(recipe_id, "failed")
        logger.exception(
            "Failed persisting extraction for recipe %s; re-raising for retry",
            recipe_id,
        )
        raise

    # Completion notification — never fails the extraction.
    _publish_notification(recipe_id, user_id, "complete", extracted["title"])

    logger.info("Extraction complete for recipe %s", recipe_id)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def handler(event, context):
    """SQS event handler.

    With a batch size of 1 the ``Records`` list contains a single message, but
    the loop handles any batch size. A raised exception causes SQS to retry;
    after ``maxReceiveCount`` attempts the message is routed to the DLQ (see
    the module docstring's retry policy).
    """
    records = event.get("Records", [])
    logger.info("Received %d record(s) for extraction", len(records))

    for record in records:
        body = record.get("body", "{}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.exception("Invalid SQS message body: %s", body)
            raise

        logger.info(
            "Extracting recipe_id=%s from s3_key=%s",
            payload.get("recipe_id"),
            payload.get("s3_key"),
        )
        _process_record(payload)

    return {"statusCode": 200, "processed": len(records)}
