"""End-to-end integration tests for the recipe extraction Lambda handler.

These drive ``extract_handler.handler`` across the *whole* pipeline — set
status 'processing' → download image → call GPT-4o → parse → persist recipe
scalars + ingredient/tool/instruction children → best-effort thumbnail → SNS
notify → status 'complete' — with every external boundary mocked so the suite
stays fully offline (no AWS, no network, no real Postgres).

The "integration" crux is a **real database**: ``extract_handler._get_engine``
is pointed at a shared-cache in-memory SQLite engine with the production
PostgreSQL ``UUID`` columns rendered as ``CHAR(36)`` (the same shim
``tests/conftest.py`` uses), and the four tables the handler writes
(``recipes``, ``ingredients``, ``tools``, ``instructions``) are created from
raw DDL mirroring ``docs/data-model.md``. The handler's own parameterised
SQLAlchemy Core statements run unmodified against that engine, so these tests
exercise the actual UPDATE/DELETE/INSERT persistence logic, not a stub.

Mocked seams (all already factored into ``extract_handler``):
  * ``_call_gpt4o``        — the "mocked OpenAI API": returns canned JSON.
  * ``_download_image``    — returns a real tiny JPEG so the real
                             ``_maybe_store_thumbnail`` (Pillow) path runs.
  * ``_get_s3_client``     — fake recording ``put_object`` (thumbnail upload).
  * ``_get_sns_client``    — fake recording ``publish`` (completion notice).

The ``lambda/`` directory ships as a flat Lambda asset (not a package), so it
is placed on ``sys.path`` and imported by name, exactly like
``tests/lambda/test_dlq_handler.py`` and the Lambda runtime itself.
"""
from __future__ import annotations

import io
import json
import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Import the flat Lambda asset by name (same approach as test_dlq_handler.py).
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_LAMBDA_DIR = os.path.join(_REPO_ROOT, "lambda")
if _LAMBDA_DIR not in sys.path:
    sys.path.insert(0, _LAMBDA_DIR)

import extract_handler  # noqa: E402  (import after sys.path manipulation)


# ---------------------------------------------------------------------------
# Schema DDL — mirrors docs/data-model.md, with UUID -> CHAR(36) for SQLite.
# ---------------------------------------------------------------------------
# Only the four tables the extraction handler touches are created. A minimal
# ``users`` table backs the recipes.user_id foreign key so seeded rows are
# referentially valid. TIMESTAMP/NUMERIC map cleanly onto SQLite's dynamic
# typing; CHECK constraints are preserved so an invalid status would be caught.
_DDL = [
    """
    CREATE TABLE users (
        id          CHAR(36) PRIMARY KEY,
        email       TEXT UNIQUE NOT NULL,
        cognito_sub TEXT UNIQUE NOT NULL,
        created_at  TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE recipes (
        id                CHAR(36) PRIMARY KEY,
        user_id           CHAR(36) NOT NULL REFERENCES users(id),
        title             TEXT NOT NULL,
        description       TEXT,
        prep_time_min     INTEGER,
        cook_time_min     INTEGER,
        total_time_min    INTEGER,
        servings          INTEGER,
        origin            TEXT,
        source_url        TEXT,
        source_citation   TEXT,
        image_s3_key      TEXT,
        thumbnail_s3_key  TEXT,
        extraction_status TEXT CHECK (extraction_status IN
                              ('pending','processing','complete','failed')),
        cook_count        INTEGER DEFAULT 0,
        avg_rating        NUMERIC(3,2),
        created_at        TIMESTAMP NOT NULL,
        updated_at        TIMESTAMP NOT NULL,
        deleted_at        TIMESTAMP
    )
    """,
    """
    CREATE TABLE ingredients (
        id          CHAR(36) PRIMARY KEY,
        recipe_id   CHAR(36) NOT NULL REFERENCES recipes(id),
        name        TEXT NOT NULL,
        quantity    NUMERIC,
        unit        TEXT,
        preparation TEXT,
        sort_order  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE tools (
        id         CHAR(36) PRIMARY KEY,
        recipe_id  CHAR(36) NOT NULL REFERENCES recipes(id),
        name       TEXT NOT NULL,
        sort_order INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE instructions (
        id          CHAR(36) PRIMARY KEY,
        recipe_id   CHAR(36) NOT NULL REFERENCES recipes(id),
        step_number INTEGER NOT NULL,
        body        TEXT NOT NULL
    )
    """,
]


def _tiny_jpeg_bytes() -> bytes:
    """Produce a small, genuinely-decodable JPEG so the real thumbnail path runs.

    ``_maybe_store_thumbnail`` opens the bytes with Pillow and re-encodes a
    400x400 JPEG; feeding it a real image means that production code path is
    exercised end-to-end rather than being mocked out.
    """
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 60)).save(buffer, format="JPEG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Fakes recording the AWS side effects.
# ---------------------------------------------------------------------------
class _FakeS3:
    """Records ``put_object`` calls so the thumbnail upload can be asserted."""

    def __init__(self) -> None:
        self.puts: list[dict] = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {}


class _FakeSns:
    """Records ``publish`` calls so the completion notification can be asserted."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "fake"}


class _Harness:
    """Bundle of the test engine + recording fakes handed to each test."""

    def __init__(self, engine, s3: _FakeS3, sns: _FakeSns) -> None:
        self.engine = engine
        self.s3 = s3
        self.sns = sns

    # -- seeding / inspection helpers ------------------------------------
    def seed_user(self, user_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, email, cognito_sub, created_at) "
                    "VALUES (:id, :email, :sub, :now)"
                ),
                {
                    "id": user_id,
                    "email": f"{user_id}@example.com",
                    "sub": f"sub-{user_id}",
                    "now": "2024-01-01 00:00:00",
                },
            )

    def seed_pending_recipe(
        self, recipe_id: str, user_id: str, image_s3_key: str
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO recipes "
                    "(id, user_id, title, origin, image_s3_key, "
                    " extraction_status, cook_count, created_at, updated_at) "
                    "VALUES "
                    "(:id, :user_id, :title, 'ios_share', :key, "
                    " 'pending', 0, :now, :now)"
                ),
                {
                    "id": recipe_id,
                    "user_id": user_id,
                    "title": "Pending recipe",
                    "key": image_s3_key,
                    "now": "2024-01-01 00:00:00",
                },
            )

    def recipe_row(self, recipe_id: str):
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT * FROM recipes WHERE id = :id"),
                {"id": recipe_id},
            ).mappings().first()

    def children(self, table: str, recipe_id: str, order_by: str):
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text(
                        f"SELECT * FROM {table} WHERE recipe_id = :id "
                        f"ORDER BY {order_by}"
                    ),
                    {"id": recipe_id},
                ).mappings()
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def harness(monkeypatch):
    """Wire ``extract_handler`` to a real in-memory SQLite DB and AWS fakes.

    * ``_get_engine`` is ``lru_cache``'d in production; we monkeypatch the
      attribute directly to return a single shared-cache engine. ``StaticPool``
      + ``check_same_thread=False`` keeps every ``engine.begin()`` /
      ``engine.connect()`` on the one in-memory database so writes persist
      across the handler's separate transactions.
    * ``S3_BUCKET`` is required by ``_process_record``; set it to a dummy.
    * S3 and SNS clients are fakes that record their calls.
    * ``get_openai_api_key`` is stubbed as a safety net — ``_call_gpt4o`` is
      patched per-test so this should never actually be reached.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    with engine.begin() as conn:
        for statement in _DDL:
            conn.execute(text(statement))

    monkeypatch.setattr(extract_handler, "_get_engine", lambda: engine)

    monkeypatch.setenv("S3_BUCKET", "test-bucket")

    fake_s3 = _FakeS3()
    fake_sns = _FakeSns()
    monkeypatch.setattr(extract_handler, "_get_s3_client", lambda: fake_s3)
    monkeypatch.setattr(extract_handler, "_get_sns_client", lambda: fake_sns)

    # Real tiny JPEG so _maybe_store_thumbnail's Pillow path runs for real.
    monkeypatch.setattr(
        extract_handler, "_download_image", lambda bucket, key: _tiny_jpeg_bytes()
    )

    # Safety net: _call_gpt4o is patched per-test, so the key path is dead code.
    monkeypatch.setattr(extract_handler, "get_openai_api_key", lambda: "test-key")

    return _Harness(engine, fake_s3, fake_sns)


def _sqs_event(recipe_id: str, s3_key: str, user_id: str) -> dict:
    """Build a single-record SQS event with the extraction message body."""
    body = json.dumps(
        {"recipe_id": recipe_id, "s3_key": s3_key, "user_id": user_id}
    )
    return {"Records": [{"body": body}]}


def _full_recipe_json() -> str:
    """A complete, well-formed extraction response (the mocked OpenAI output)."""
    return json.dumps(
        {
            "title": "Garlic Butter Pasta",
            "description": "A quick weeknight pasta.",
            "prep_time_min": 10,
            "cook_time_min": 15,
            "total_time_min": 25,
            "servings": 4,
            "ingredients": [
                {
                    "name": "spaghetti",
                    "quantity": 400,
                    "unit": "g",
                    "preparation": None,
                },
                {
                    "name": "salt",
                    "quantity": None,  # null quantity -> stored as NULL
                    "unit": None,
                    "preparation": "to taste",
                },
            ],
            "tools": ["pot", "colander"],
            "instructions": [
                "Boil the pasta.",
                "Melt the butter with garlic.",
                "Toss together and serve.",
            ],
        }
    )


# ---------------------------------------------------------------------------
# 1) Happy path
# ---------------------------------------------------------------------------
def test_happy_path_extracts_recipe_children_thumbnail_and_notifies(
    harness, monkeypatch
):
    """A full extraction populates the recipe, children, thumbnail, and SNS."""
    user_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())
    harness.seed_user(user_id)
    harness.seed_pending_recipe(recipe_id, user_id, "uploads/photo.jpg")

    monkeypatch.setenv(
        "SNS_NOTIFICATIONS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:recipes"
    )
    monkeypatch.setattr(
        extract_handler, "_call_gpt4o", lambda image_bytes: _full_recipe_json()
    )

    result = extract_handler.handler(
        _sqs_event(recipe_id, "uploads/photo.jpg", user_id), context=None
    )
    assert result == {"statusCode": 200, "processed": 1}

    # Recipe scalars + status.
    row = harness.recipe_row(recipe_id)
    assert row["extraction_status"] == "complete"
    assert row["title"] == "Garlic Butter Pasta"
    assert row["description"] == "A quick weeknight pasta."
    assert row["prep_time_min"] == 10
    assert row["cook_time_min"] == 15
    assert row["total_time_min"] == 25
    assert row["servings"] == 4
    assert row["thumbnail_s3_key"] == "thumbnails/photo.jpg"

    # Ingredients: 2, ordered by sort_order, null quantity preserved.
    ingredients = harness.children("ingredients", recipe_id, "sort_order")
    assert [i["sort_order"] for i in ingredients] == [0, 1]
    assert [i["name"] for i in ingredients] == ["spaghetti", "salt"]
    assert float(ingredients[0]["quantity"]) == 400.0
    assert ingredients[1]["quantity"] is None
    assert ingredients[1]["preparation"] == "to taste"

    # Tools: 2, ordered.
    tools = harness.children("tools", recipe_id, "sort_order")
    assert [t["sort_order"] for t in tools] == [0, 1]
    assert [t["name"] for t in tools] == ["pot", "colander"]

    # Instructions: 3, step_number 1..3.
    instructions = harness.children("instructions", recipe_id, "step_number")
    assert [s["step_number"] for s in instructions] == [1, 2, 3]
    assert instructions[0]["body"] == "Boil the pasta."

    # Thumbnail uploaded.
    assert len(harness.s3.puts) == 1
    put = harness.s3.puts[0]
    assert put["Bucket"] == "test-bucket"
    assert put["Key"] == "thumbnails/photo.jpg"
    assert put["ContentType"] == "image/jpeg"
    assert isinstance(put["Body"], (bytes, bytearray)) and put["Body"]

    # SNS completion notification.
    assert len(harness.sns.published) == 1
    message = json.loads(harness.sns.published[0]["Message"])
    assert message == {
        "recipe_id": recipe_id,
        "user_id": user_id,
        "status": "complete",
        "title": "Garlic Butter Pasta",
    }


# ---------------------------------------------------------------------------
# 2) Partial extraction
# ---------------------------------------------------------------------------
def test_partial_extraction_is_handled_gracefully(harness, monkeypatch):
    """Missing title, non-numeric quantity, and blank steps degrade cleanly."""
    user_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())
    harness.seed_user(user_id)
    harness.seed_pending_recipe(recipe_id, user_id, "uploads/partial.png")

    partial = json.dumps(
        {
            # title omitted entirely -> DEFAULT_TITLE
            "description": None,
            "ingredients": [
                {"name": "flour", "quantity": "a pinch", "unit": "cup"},
                {"name": "", "quantity": 1},  # nameless -> dropped
            ],
            "tools": ["bowl", "", None],  # blanks dropped
            "instructions": ["Mix.", "", "   ", "Bake."],  # blanks dropped
        }
    )
    monkeypatch.setattr(extract_handler, "_call_gpt4o", lambda b: partial)

    result = extract_handler.handler(
        _sqs_event(recipe_id, "uploads/partial.png", user_id), context=None
    )
    assert result == {"statusCode": 200, "processed": 1}

    row = harness.recipe_row(recipe_id)
    assert row["extraction_status"] == "complete"
    assert row["title"] == extract_handler.DEFAULT_TITLE  # "Untitled recipe"
    assert row["description"] is None

    ingredients = harness.children("ingredients", recipe_id, "sort_order")
    assert len(ingredients) == 1  # nameless entry dropped
    assert ingredients[0]["name"] == "flour"
    assert ingredients[0]["quantity"] is None  # "a pinch" -> NULL

    tools = harness.children("tools", recipe_id, "sort_order")
    assert [t["name"] for t in tools] == ["bowl"]  # blanks dropped

    instructions = harness.children("instructions", recipe_id, "step_number")
    assert [s["body"] for s in instructions] == ["Mix.", "Bake."]
    assert [s["step_number"] for s in instructions] == [1, 2]


# ---------------------------------------------------------------------------
# 3) Unparseable LLM response -> transient: handler raises, row is 'failed'
# ---------------------------------------------------------------------------
def test_unparseable_response_marks_failed_and_raises_for_retry(
    harness, monkeypatch
):
    """A garbage (non-JSON) response flips the row to failed, then re-raises."""
    user_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())
    harness.seed_user(user_id)
    harness.seed_pending_recipe(recipe_id, user_id, "uploads/bad.jpg")

    monkeypatch.setattr(extract_handler, "_call_gpt4o", lambda b: "not json")

    with pytest.raises(ValueError):
        extract_handler.handler(
            _sqs_event(recipe_id, "uploads/bad.jpg", user_id), context=None
        )

    # Row was flipped to failed before the re-raise.
    row = harness.recipe_row(recipe_id)
    assert row["extraction_status"] == "failed"

    # No children, no thumbnail, no notification on a failed parse.
    assert harness.children("ingredients", recipe_id, "sort_order") == []
    assert harness.s3.puts == []
    assert harness.sns.published == []


# ---------------------------------------------------------------------------
# 4) Bad input — recipe missing -> returns normally, nothing inserted
# ---------------------------------------------------------------------------
def test_missing_recipe_returns_without_raise_or_inserts(harness, monkeypatch):
    """An event for an unknown recipe_id is dropped: no raise, no writes."""
    missing_id = str(uuid.uuid4())

    # _call_gpt4o must never be reached; make it blow up if it is.
    def _must_not_call(_bytes):  # pragma: no cover - asserted via no side effects
        raise AssertionError("_call_gpt4o should not run for a missing recipe")

    monkeypatch.setattr(extract_handler, "_call_gpt4o", _must_not_call)

    result = extract_handler.handler(
        _sqs_event(missing_id, "uploads/ghost.jpg", "user-x"), context=None
    )
    assert result == {"statusCode": 200, "processed": 1}

    assert harness.recipe_row(missing_id) is None
    assert harness.children("ingredients", missing_id, "sort_order") == []
    assert harness.s3.puts == []
    assert harness.sns.published == []


# ---------------------------------------------------------------------------
# 5) Idempotent re-run -> children are not duplicated
# ---------------------------------------------------------------------------
def test_rerun_is_idempotent_children_not_duplicated(harness, monkeypatch):
    """Running the happy path twice leaves exactly one set of children."""
    user_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())
    harness.seed_user(user_id)
    harness.seed_pending_recipe(recipe_id, user_id, "uploads/photo.jpg")

    monkeypatch.setattr(
        extract_handler, "_call_gpt4o", lambda b: _full_recipe_json()
    )
    event = _sqs_event(recipe_id, "uploads/photo.jpg", user_id)

    extract_handler.handler(event, context=None)
    extract_handler.handler(event, context=None)

    # _persist_extraction deletes-then-inserts, so counts stay flat.
    assert len(harness.children("ingredients", recipe_id, "sort_order")) == 2
    assert len(harness.children("tools", recipe_id, "sort_order")) == 2
    assert len(harness.children("instructions", recipe_id, "step_number")) == 3

    row = harness.recipe_row(recipe_id)
    assert row["extraction_status"] == "complete"


# ---------------------------------------------------------------------------
# 6) SNS absent -> completes, no publish attempted
# ---------------------------------------------------------------------------
def test_no_sns_topic_still_completes_without_publishing(harness, monkeypatch):
    """With the topic env unset, extraction completes and nothing is published."""
    monkeypatch.delenv("SNS_NOTIFICATIONS_TOPIC_ARN", raising=False)

    user_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())
    harness.seed_user(user_id)
    harness.seed_pending_recipe(recipe_id, user_id, "uploads/photo.jpg")

    monkeypatch.setattr(
        extract_handler, "_call_gpt4o", lambda b: _full_recipe_json()
    )

    result = extract_handler.handler(
        _sqs_event(recipe_id, "uploads/photo.jpg", user_id), context=None
    )
    assert result == {"statusCode": 200, "processed": 1}

    row = harness.recipe_row(recipe_id)
    assert row["extraction_status"] == "complete"
    assert harness.sns.published == []  # no topic -> no publish
