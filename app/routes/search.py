"""Search resource endpoints.

Houses the ``/v1/search`` surface. This branch implements the AI-powered
natural-language endpoint ``POST /search/llm`` (TASK-3.2). The heuristic filter
endpoint ``GET /search`` (TASK-3.1) lives on a separate branch and is not
present here.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app.auth import current_user, require_auth
from app.schemas.search import LlmSearchRequest
from app.services import llm_search

search_bp = Blueprint("search", __name__, url_prefix="/v1/search")


def _validation_error_message(error: ValidationError) -> str:
    """Flatten a pydantic ``ValidationError`` into one concise 400 message.

    Mirrors the helper in :mod:`app.routes.recipes`: reports the first problem
    as ``"<field>: <reason>"`` (or just the reason for a model-level error) so
    the ``{"error": ...}`` body stays short and human-readable.
    """
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = first.get("msg", "invalid request")
    return f"{location}: {message}" if location else message


@search_bp.post("/llm")
@require_auth
def llm_search_view():
    """AI-powered natural-language recipe search.

    Implements ``POST /search/llm`` from ``docs/api.md``. Accepts a
    ``{"query": "..."}`` body, ranks the caller's own non-deleted recipes
    against the query using an LLM, and returns recipe cards each with a
    ``match_explanation``. Results are cached in Redis for five minutes keyed by
    ``{user_id}:{sha256(query)}`` so an identical repeat query does not re-hit
    the model. If the LLM call fails for any reason the endpoint falls back to a
    self-contained heuristic token match (that result is not cached).

    Validation errors (missing/empty ``query``, over-length, unknown fields)
    are rejected with HTTP 400 and a concise ``{"error": <message>}`` body.

    Returns:
        JSON ``{"recipes": [<card with match_explanation>, ...]}`` with HTTP
        200. The order of ``recipes`` is the match ranking (best first).
    """
    try:
        payload = LlmSearchRequest.model_validate(request.get_json(silent=True) or {})
    except ValidationError as error:
        return jsonify({"error": _validation_error_message(error)}), 400

    response = llm_search.search_recipes(current_user.id, payload.query)
    return jsonify(response), 200
