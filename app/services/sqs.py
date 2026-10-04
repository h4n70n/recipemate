"""SQS helper for enqueuing async recipe-extraction jobs.

This is the producer side of the async extraction pipeline. When a client
calls ``POST /recipes/extract`` the API creates a ``pending`` recipe row and
drops a small message onto the extraction queue; a separate Lambda consumer
(see ``lambda/extract_handler.py``) picks the message up, runs the LLM
extraction against the uploaded image, and overwrites the recipe's fields and
``extraction_status`` when it finishes.

Message schema
--------------
Each message body is a JSON object with exactly these keys::

    {
        "recipe_id": "<uuid>",   # the pending recipe to populate
        "s3_key":    "<key>",    # the uploaded image/document to extract from
        "user_id":   "<uuid>"    # owner of the recipe (for attribution/limits)
    }

UUID values are stringified so the body is plain JSON. The consuming Lambda
relies on this exact shape; keep it in sync with ``lambda/extract_handler.py``.

boto3 client
------------
:func:`get_sqs_client` builds an SQS client honouring the app's ``AWS_REGION``
and the optional ``AWS_ENDPOINT_URL`` override — mirroring
:func:`app.services.s3.get_s3_client` so local development can target
LocalStack (``AWS_ENDPOINT_URL=http://localhost:4566``) while deployed
environments leave it empty and hit real AWS. Both the client factory and
:func:`enqueue_extraction` are deliberately kept as single, easily
monkeypatched seams so tests can mock the queue without a running broker.
"""

from __future__ import annotations

import json

import boto3
from flask import current_app


def get_sqs_client():
    """Build an SQS client from the active app config.

    Honours ``AWS_REGION`` and, when set, ``AWS_ENDPOINT_URL`` (so local
    development can target LocalStack). Written as a single seam so tests can
    monkeypatch it with a fake client.

    Returns:
        A configured boto3 SQS client.
    """
    region = current_app.config.get("AWS_REGION") or "us-east-2"
    endpoint_url = current_app.config.get("AWS_ENDPOINT_URL") or None

    return boto3.client(
        "sqs",
        region_name=region,
        endpoint_url=endpoint_url,
    )


def _require_queue_url() -> str:
    """Return the configured extraction queue URL or raise a clear error.

    Returns:
        The ``SQS_EXTRACTION_QUEUE_URL`` config value.

    Raises:
        RuntimeError: If no queue URL is configured, so a missing/empty
            ``SQS_EXTRACTION_QUEUE_URL`` fails loudly here rather than letting
            boto3 raise an opaque error against an empty queue URL.
    """
    queue_url = current_app.config.get("SQS_EXTRACTION_QUEUE_URL")
    if not queue_url:
        raise RuntimeError("SQS_EXTRACTION_QUEUE_URL is not configured")
    return queue_url


def enqueue_extraction(recipe_id: object, s3_key: str, user_id: object) -> str:
    """Enqueue an extraction job for a freshly created pending recipe.

    Sends a JSON message ``{"recipe_id", "s3_key", "user_id"}`` (see the module
    docstring for the schema the Lambda consumes) to the configured extraction
    queue. UUID arguments are stringified so the body serialises cleanly.

    Args:
        recipe_id: The id of the ``pending`` recipe the consumer will populate.
        s3_key: The uploaded object key to extract the recipe from.
        user_id: The owning user's id (for attribution / rate limiting).

    Returns:
        The SQS ``MessageId`` of the sent message.

    Raises:
        RuntimeError: If ``SQS_EXTRACTION_QUEUE_URL`` is not configured.
        botocore.exceptions.ClientError: If the send fails (propagated so the
            caller can decide how to handle an enqueue failure).
    """
    queue_url = _require_queue_url()
    body = json.dumps(
        {
            "recipe_id": str(recipe_id),
            "s3_key": s3_key,
            "user_id": str(user_id),
        }
    )

    client = get_sqs_client()
    response = client.send_message(QueueUrl=queue_url, MessageBody=body)
    return response["MessageId"]
