"""
Dead-letter queue (DLQ) handler for the recipe-extraction pipeline.

The main extraction Lambda (:mod:`extract_handler`) re-raises on transient
failure so SQS retries and, after ``maxReceiveCount`` receives, routes the
message to the extraction DLQ.  Without a consumer, a dead-lettered message
would simply sit in the DLQ until its 14-day retention expired — the failure
would be invisible to operators and, in the rare case the main handler crashed
hard *before* flipping ``extraction_status`` to ``'failed'``, the recipe row
could be left stuck at ``'pending'``/``'processing'`` forever.

This Lambda is the DLQ consumer.  For each dead-lettered message it:

  1. Decodes the body ``{"recipe_id","s3_key","user_id"}``.  A malformed body
     is logged and skipped — a DLQ handler must never itself dead-letter.
  2. Defensively marks the referenced recipe ``extraction_status='failed'``
     (only if the row exists and is not already terminal ``'failed'``), bumping
     ``updated_at``.  The main handler usually already did this; we repeat it so
     a hard crash cannot leave a recipe wedged in a non-terminal state.
  3. Logs the failure prominently at ERROR level with the recipe id and the
     original message body, so operators can alarm on DLQ activity in
     CloudWatch.
  4. Best-effort publishes an SNS notification (``status='failed'``) so the push
     path can tell the user extraction failed — only when
     ``SNS_NOTIFICATIONS_TOPIC_ARN`` is set, and never raising on failure.

The handler always returns normally so every DLQ message is consumed and
removed after handling (a DLQ for a DLQ is not configured, and re-dead-lettering
a terminal failure buys nothing).

Environment variables
----------------------
  - DATABASE_URL                : PostgreSQL connection string.
  - SNS_NOTIFICATIONS_TOPIC_ARN : (optional) SNS topic to publish failure to.

Design — reuse the extraction handler's seams
----------------------------------------------
``dlq_handler`` and ``extract_handler`` are colocated in ``lambda/`` and ship in
the same asset, so this module imports :func:`extract_handler._get_engine`,
:func:`extract_handler._recipe_exists`, and
:func:`extract_handler._publish_notification` rather than duplicating the
decoupled SQLAlchemy-Core / lazy-boto3 plumbing.  The one thing it does *not*
reuse is status-setting: :func:`extract_handler._set_status` is an unconditional
UPDATE, whereas the DLQ needs an idempotent, terminal-aware set that reads the
current status first.  That lives here as :func:`_mark_failed`.

Everything is lazy (engine, SNS client are built inside functions, never at
import time) so the module imports cleanly with no DB driver, no network, and no
AWS credentials — the same discipline as :mod:`extract_handler`, which keeps the
whole thing unit-testable by monkeypatching the small seams below.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: Terminal extraction status.  A recipe already here is left untouched (the
#: main handler marked it); anything else is defensively flipped to it.
TERMINAL_FAILED = "failed"


# ---------------------------------------------------------------------------
# DB — terminal-aware, idempotent failure marking
# ---------------------------------------------------------------------------


def _mark_failed(recipe_id: str) -> bool:
    """Defensively set ``extraction_status='failed'`` for ``recipe_id``.

    Reuses :func:`extract_handler._get_engine` (so the DLQ handler shares one
    pooled engine discipline and the ``DATABASE_URL`` wiring) and issues a
    parameterised, terminal-aware UPDATE in its own short transaction:

      * If the recipe row does not exist, this is a no-op and returns ``False``
        (nothing to mark — the row was likely hard-deleted).
      * If the row is already terminal ``'failed'``, it is left untouched and
        returns ``False`` (the main handler already recorded the failure).
      * Otherwise it flips the status to ``'failed'``, bumps ``updated_at``, and
        returns ``True``.

    Raising is deliberately avoided at the call site (see :func:`handler`): a
    DB error here is logged and swallowed so the DLQ message is still consumed.
    """
    import extract_handler
    from sqlalchemy import text

    engine = extract_handler._get_engine()
    now = datetime.datetime.utcnow()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT extraction_status FROM recipes WHERE id = :recipe_id"),
            {"recipe_id": recipe_id},
        ).first()
        if row is None:
            return False
        if row[0] == TERMINAL_FAILED:
            return False
        conn.execute(
            text(
                "UPDATE recipes "
                "SET extraction_status = :status, updated_at = :now "
                "WHERE id = :recipe_id"
            ),
            {"status": TERMINAL_FAILED, "now": now, "recipe_id": recipe_id},
        )
    return True


# ---------------------------------------------------------------------------
# SNS — best-effort failure notification
# ---------------------------------------------------------------------------


def _notify_failed(recipe_id: str, user_id: str | None) -> None:
    """Publish a ``status='failed'`` notification, best-effort.

    Delegates to :func:`extract_handler._publish_notification`, which no-ops
    when ``SNS_NOTIFICATIONS_TOPIC_ARN`` is unset and swallows any publish
    error.  ``title`` is ``None`` because a dead-lettered recipe never produced
    a title; the push consumer renders a generic "extraction failed" message.
    """
    import extract_handler

    extract_handler._publish_notification(recipe_id, user_id, "failed", None)


# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------


def _process_record(body: str) -> None:
    """Handle a single dead-lettered SQS message body.

    Decodes the JSON body, marks the recipe failed (defensively), logs the
    dead-letter prominently, and attempts a best-effort failure notification.
    A malformed body is logged and skipped. This function never raises: all
    foreseeable errors (bad JSON, DB error, SNS error) are caught here so the
    batch loop in :func:`handler` always completes and the message is consumed.
    """
    try:
        payload: dict[str, Any] = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("DLQ message body was not a JSON object")
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        # A malformed body cannot be acted on. Log and move on — never raise,
        # or the DLQ handler would itself fail the batch repeatedly.
        logger.error(
            "Discarding malformed DLQ message (cannot decode): %r (%s)",
            body,
            error,
        )
        return

    recipe_id = payload.get("recipe_id")
    user_id = payload.get("user_id")

    if not recipe_id:
        logger.error(
            "DLQ message has no recipe_id; nothing to mark failed: %r",
            payload,
        )
        return

    # Prominent, structured error log so operators can alarm on DLQ activity.
    logger.error(
        "Extraction permanently failed (dead-lettered) for recipe_id=%s; "
        "original message: %r",
        recipe_id,
        payload,
    )

    # Defensively mark the recipe failed. Swallow DB errors so a transient DB
    # blip does not stop the message being consumed off the DLQ.
    try:
        changed = _mark_failed(recipe_id)
        if changed:
            logger.info(
                "Marked recipe %s extraction_status=failed from DLQ handler",
                recipe_id,
            )
        else:
            logger.info(
                "Recipe %s already terminal or missing; no status change",
                recipe_id,
            )
    except Exception as error:  # noqa: BLE001 - must not fail the DLQ consume
        logger.error(
            "Could not mark recipe %s failed from DLQ (continuing): %s",
            recipe_id,
            error,
        )

    # Best-effort failure notification (no-ops without a topic; never raises).
    _notify_failed(recipe_id, user_id)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def handler(event, context):
    """SQS event handler for the extraction dead-letter queue.

    Processes every record in the batch and always returns normally so SQS
    deletes the messages. No record is allowed to raise (see
    :func:`_process_record`), so one bad message never blocks the rest of the
    batch and the DLQ consumer never re-dead-letters.
    """
    records = event.get("Records", [])
    logger.info("Received %d dead-lettered record(s)", len(records))

    for record in records:
        body = record.get("body", "{}")
        _process_record(body)

    return {"statusCode": 200, "processed": len(records)}
