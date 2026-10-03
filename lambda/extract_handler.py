"""
Recipe extraction Lambda handler.

Triggered by SQS messages enqueued when a user requests extraction of an
uploaded image.  Each message references an S3 key and a recipe id.  The
handler downloads the image from S3, calls the GPT-4o vision API with a
structured extraction prompt, parses the structured JSON response, and
updates the recipe record in PostgreSQL.

Environment variables:
  - OPENAI_SECRET_NAME     : Secrets Manager name holding the OpenAI API key.
  - OPENAI_SECRET_JSON_KEY : JSON key within that secret (default OPENAI_API_KEY).
  - S3_BUCKET              : Name of the bucket holding uploaded recipe images.
  - DATABASE_URL           : PostgreSQL connection string.

The OpenAI key is fetched from Secrets Manager at runtime (and cached for the
lifetime of the warm container) rather than injected as a plaintext env var, so
the key never appears in the function's configuration or the deploy template.

This is an asset placeholder so the CDK ``Code.from_asset("lambda")``
reference resolves at synth time.  The full extraction logic is implemented
in TASK-2.3.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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


def handler(event, context):
    """
    SQS event handler.

    With a batch size of 1 the ``Records`` list contains a single message.
    Raising an exception causes SQS to retry; after ``maxReceiveCount``
    attempts the message is routed to the dead-letter queue.
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

        recipe_id = payload.get("recipe_id")
        s3_key = payload.get("s3_key")
        logger.info("Extracting recipe_id=%s from s3_key=%s", recipe_id, s3_key)

        # TODO (TASK-2.3): download image from S3, call GPT-4o vision, parse
        # structured JSON, update the recipe record, publish SNS notification.

    return {"statusCode": 200, "processed": len(records)}
