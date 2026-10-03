"""Unit tests for the extraction dead-letter-queue consumer.

The DLQ handler (``lambda/dlq_handler.py``) talks to the DB via
``extract_handler``'s engine helpers and to SNS via boto3. These tests keep
everything import-only and offline: the two externally-effecting seams —
:func:`dlq_handler._mark_failed` (DB) and :func:`dlq_handler._notify_failed`
(SNS) — are monkeypatched with recording fakes, a synthetic SQS event is fed to
the handler, and behaviour is asserted. No real AWS, no DB driver, no network.

What is covered (TASK-2.3 "Dead-letter queue handling for failed extractions"):
  * a well-formed record marks the recipe failed with the right id;
  * a malformed / garbage body does NOT raise and is skipped (never marked);
  * SNS publish is attempted when the topic env is set and skipped otherwise;
  * the handler returns normally for a whole batch.
"""
from __future__ import annotations

import importlib
import json
import os
import sys

import pytest

# The ``lambda/`` directory ships as a flat Lambda asset (not a Python
# package), so put it on sys.path and import the module by name — the same way
# the Lambda runtime loads ``dlq_handler.handler``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_LAMBDA_DIR = os.path.join(_REPO_ROOT, "lambda")
if _LAMBDA_DIR not in sys.path:
    sys.path.insert(0, _LAMBDA_DIR)

import dlq_handler  # noqa: E402  (import after sys.path manipulation)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sqs_event(*bodies: str) -> dict:
    """Build a synthetic SQS event with one record per supplied body string."""
    return {"Records": [{"body": body} for body in bodies]}


def _body(recipe_id, user_id="user-1", s3_key="uploads/x.jpg") -> str:
    """Serialise a well-formed DLQ message body."""
    return json.dumps(
        {"recipe_id": recipe_id, "s3_key": s3_key, "user_id": user_id}
    )


@pytest.fixture()
def seams(monkeypatch):
    """Patch the DB and SNS seams with recording fakes.

    Returns an object exposing ``marked`` (recipe ids passed to _mark_failed)
    and ``notified`` (``(recipe_id, user_id)`` tuples passed to _notify_failed).
    """

    class Recorder:
        def __init__(self):
            self.marked: list[str] = []
            self.notified: list[tuple] = []

    rec = Recorder()

    def fake_mark_failed(recipe_id):
        rec.marked.append(recipe_id)
        return True  # pretend the row existed and was flipped

    def fake_notify_failed(recipe_id, user_id):
        rec.notified.append((recipe_id, user_id))

    monkeypatch.setattr(dlq_handler, "_mark_failed", fake_mark_failed)
    monkeypatch.setattr(dlq_handler, "_notify_failed", fake_notify_failed)
    return rec


# ---------------------------------------------------------------------------
# Module import discipline
# ---------------------------------------------------------------------------


def test_module_imports_without_aws_or_db(monkeypatch):
    """The module imports cleanly with no AWS creds and no DATABASE_URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SNS_NOTIFICATIONS_TOPIC_ARN", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    reloaded = importlib.reload(dlq_handler)
    assert callable(reloaded.handler)


# ---------------------------------------------------------------------------
# Well-formed record -> recipe marked failed
# ---------------------------------------------------------------------------


def test_wellformed_record_marks_recipe_failed(seams):
    """A valid DLQ body flips the referenced recipe to failed."""
    event = _sqs_event(_body("recipe-123"))

    result = dlq_handler.handler(event, context=None)

    assert seams.marked == ["recipe-123"]
    assert result == {"statusCode": 200, "processed": 1}


# ---------------------------------------------------------------------------
# Malformed body -> no raise, skipped, not marked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_body",
    [
        "not json at all",
        "{broken json",
        "[1, 2, 3]",          # valid JSON, but not an object
        "\"just a string\"",  # valid JSON, but not an object
        "42",                 # valid JSON, but not an object
        "{}",                 # object but no recipe_id
        json.dumps({"s3_key": "uploads/x.jpg"}),  # missing recipe_id
    ],
)
def test_malformed_body_does_not_raise_and_is_skipped(seams, bad_body):
    """Garbage / incomplete bodies are logged and skipped, never raised."""
    event = _sqs_event(bad_body)

    # Must not raise.
    result = dlq_handler.handler(event, context=None)

    # Nothing was marked and nothing was notified for an unusable message.
    assert seams.marked == []
    assert seams.notified == []
    assert result == {"statusCode": 200, "processed": 1}


# ---------------------------------------------------------------------------
# SNS publish attempted iff topic configured
# ---------------------------------------------------------------------------


def test_sns_notification_attempted_when_topic_set(seams):
    """A well-formed record triggers a best-effort failure notification."""
    event = _sqs_event(_body("recipe-abc", user_id="user-9"))

    dlq_handler.handler(event, context=None)

    assert seams.notified == [("recipe-abc", "user-9")]


def test_sns_skipped_for_unusable_message(seams):
    """A message with no recipe_id never attempts a notification."""
    event = _sqs_event(json.dumps({"s3_key": "uploads/x.jpg"}))

    dlq_handler.handler(event, context=None)

    assert seams.notified == []


def test_publish_notification_noops_without_topic(monkeypatch):
    """_notify_failed is a no-op (no publish) when the topic env is unset.

    This exercises the real ``_notify_failed`` -> ``extract_handler.
    _publish_notification`` path with the SNS client patched, proving the
    topic-gating: no topic ARN => no publish call.
    """
    import extract_handler

    monkeypatch.delenv("SNS_NOTIFICATIONS_TOPIC_ARN", raising=False)

    published: list = []

    class FakeSns:
        def publish(self, **kwargs):  # pragma: no cover - must not be called
            published.append(kwargs)

    monkeypatch.setattr(extract_handler, "_get_sns_client", lambda: FakeSns())

    dlq_handler._notify_failed("recipe-x", "user-x")

    assert published == []


def test_publish_notification_publishes_failed_when_topic_set(monkeypatch):
    """_notify_failed publishes a status='failed' message when a topic is set."""
    import extract_handler

    monkeypatch.setenv("SNS_NOTIFICATIONS_TOPIC_ARN", "arn:aws:sns:::topic")

    published: list = []

    class FakeSns:
        def publish(self, **kwargs):
            published.append(kwargs)

    monkeypatch.setattr(extract_handler, "_get_sns_client", lambda: FakeSns())

    dlq_handler._notify_failed("recipe-x", "user-x")

    assert len(published) == 1
    message = json.loads(published[0]["Message"])
    assert message == {
        "recipe_id": "recipe-x",
        "user_id": "user-x",
        "status": "failed",
        "title": None,
    }


# ---------------------------------------------------------------------------
# Batch processing returns normally
# ---------------------------------------------------------------------------


def test_batch_of_mixed_records_returns_normally(seams):
    """A batch mixing good and bad records processes all and returns normally.

    The two well-formed records are marked; the malformed one is skipped. The
    handler returns the full processed count and never raises.
    """
    event = _sqs_event(
        _body("recipe-1"),
        "garbage-not-json",
        _body("recipe-2", user_id="user-2"),
    )

    result = dlq_handler.handler(event, context=None)

    assert seams.marked == ["recipe-1", "recipe-2"]
    assert seams.notified == [("recipe-1", "user-1"), ("recipe-2", "user-2")]
    assert result == {"statusCode": 200, "processed": 3}


def test_empty_batch_returns_zero_processed(seams):
    """An event with no records returns processed=0 and does nothing."""
    result = dlq_handler.handler({"Records": []}, context=None)

    assert seams.marked == []
    assert result == {"statusCode": 200, "processed": 0}


def test_mark_failed_db_error_is_swallowed(monkeypatch):
    """A DB error in _mark_failed does not stop the message being consumed."""
    def boom(recipe_id):
        raise RuntimeError("db unavailable")

    notified: list = []
    monkeypatch.setattr(dlq_handler, "_mark_failed", boom)
    monkeypatch.setattr(
        dlq_handler,
        "_notify_failed",
        lambda rid, uid: notified.append((rid, uid)),
    )

    event = _sqs_event(_body("recipe-err"))

    # Must not raise despite the DB error.
    result = dlq_handler.handler(event, context=None)

    assert result == {"statusCode": 200, "processed": 1}
    # Notification is still best-effort attempted after a DB failure.
    assert notified == [("recipe-err", "user-1")]
