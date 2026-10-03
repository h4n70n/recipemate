"""Search resource endpoints.

Houses the ``/v1/search`` surface documented in ``docs/api.md``:

* ``GET /search`` (TASK-3.1) — heuristic structured filter/browse over the
  authenticated caller's own recipes. All query params are optional and combine
  with AND logic; the result is the same recipe-card shape as ``GET /recipes``
  wrapped in the ``{"recipes": [...], "total": N}`` envelope.
* ``POST /search/llm`` (TASK-3.2) — AI natural-language ranking of the caller's
  recipes, returning cards each with a ``match_explanation`` and falling back to
  a self-contained heuristic when the LLM call fails.

Heuristic design notes (``GET /search``)
----------------------------------------
* **Scope** mirrors ``GET /recipes``: only the caller's own, non-soft-deleted
  recipes are searched.
* **Multi-value (ALL) semantics**: ``ingredient``/``tag``/``tool`` are
  repeatable and a recipe must match *every* listed value. This is implemented
  as one correlated ``EXISTS`` subquery *per value* (not a single ``IN``, which
  would be OR) so all values must independently match. See
  :class:`~app.schemas.search.SearchQuery` for the reconciliation of the task's
  "OR across values"/"ALL of them" phrasing — ALL/AND wins.
* **Name matching** for those three filters is case-insensitive *exact* match
  on the stored name via ``func.lower(name) == value.lower()``. Exact (not
  substring) keeps the filter predictable — ``?tag=italian`` matches ``Italian``
  but not ``Italian-American``.
* **``q``** is the one substring filter: a case-insensitive match on the title,
  implemented with an explicit ``func.lower(title) LIKE %q%`` so case-folding is
  deterministic on both Postgres and the SQLite test DB (SQLite's ``LIKE`` is
  only case-insensitive for ASCII and ``ILIKE`` is not native there).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError
from sqlalchemy import func

from app.auth import current_user, require_auth
from app.models.recipe import Ingredient, Recipe, RecipeTag, Tag, Tool
from app.schemas.recipe import RecipeCard, RecipeListResponse
from app.schemas.search import LlmSearchRequest, SearchQuery
from app.services import llm_search
from app.services.urls import s3_key_to_url

search_bp = Blueprint("search", __name__, url_prefix="/v1/search")


def _validation_error_message(error: ValidationError) -> str:
    """Flatten a pydantic ``ValidationError`` into one concise 400 message.

    Mirrors the helper in :mod:`app.routes.recipes`: reports the first problem
    as ``"<field>: <reason>"`` (or just the reason for a model-level error) so
    the ``{"error": ...}`` body stays short and human-readable. Shared by both
    the heuristic and LLM search views.
    """
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = first.get("msg", "invalid request")
    return f"{location}: {message}" if location else message


# ---------------------------------------------------------------------------
# Heuristic filter search (GET /search) — TASK-3.1
# ---------------------------------------------------------------------------


def _like_escape(value: str) -> str:
    """Escape ``LIKE`` wildcards in a user string for a literal substring match.

    ``q`` is a literal substring search, so a ``%`` or ``_`` the user typed must
    be matched literally rather than acting as a wildcard. We escape them (and
    the escape char itself) and pair the pattern with ``escape="\\"`` below.
    """
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _apply_filters(query, params: SearchQuery):
    """Apply each *present* filter from ``params`` to ``query``, composably.

    Takes the base owned/non-deleted ``Recipe`` query and layers on only the
    filters the caller actually supplied, returning the narrowed query. Each
    filter is independent, so any combination composes with AND semantics.

    Args:
        query: The base SQLAlchemy query (already scoped to the owner and
            ``deleted_at IS NULL``).
        params: The validated :class:`SearchQuery`.

    Returns:
        The filtered query (not yet ordered or paginated).
    """
    # max_time: total_time_min must be present and within the budget. The
    # ``is not None`` guard excludes recipes with an unknown total time rather
    # than letting a NULL silently drop out of (or into) the comparison.
    if params.max_time is not None:
        query = query.filter(
            Recipe.total_time_min.isnot(None),
            Recipe.total_time_min <= params.max_time,
        )

    # q: case-insensitive substring on title. Explicit lower() + escaped LIKE
    # so folding is deterministic across Postgres and SQLite.
    if params.q is not None:
        pattern = f"%{_like_escape(params.q.lower())}%"
        query = query.filter(
            func.lower(Recipe.title).like(pattern, escape="\\")
        )

    # ingredient (ALL): one correlated EXISTS per value so every listed
    # ingredient must be present on the recipe. Case-insensitive exact name.
    for name in params.ingredients:
        query = query.filter(
            Ingredient.query.filter(
                Ingredient.recipe_id == Recipe.id,
                func.lower(Ingredient.name) == name.lower(),
            ).exists()
        )

    # tool (ALL): same approach against Tool.
    for name in params.tools:
        query = query.filter(
            Tool.query.filter(
                Tool.recipe_id == Recipe.id,
                func.lower(Tool.name) == name.lower(),
            ).exists()
        )

    # tag (ALL): same approach, joining recipe_tags -> tags per value.
    for name in params.tags:
        query = query.filter(
            RecipeTag.query.join(Tag, Tag.id == RecipeTag.tag_id)
            .filter(
                RecipeTag.recipe_id == Recipe.id,
                func.lower(Tag.name) == name.lower(),
            )
            .exists()
        )

    return query


def _apply_sort(query, sort: str):
    """Apply the requested sort order to ``query``.

    Sort options (default ``newest``):

    * ``newest`` -> ``created_at`` descending.
    * ``oldest`` -> ``created_at`` ascending.
    * ``most_cooked`` -> ``cook_count`` descending, ``created_at`` descending
      as a stable tiebreak.
    * ``highest_rated`` -> ``avg_rating`` descending with NULLs last, then
      ``created_at`` descending as a tiebreak. **Null handling**: recipes with
      no rating yet (``avg_rating IS NULL``) sort *after* every rated recipe
      rather than being treated as a top or bottom numeric value. This is
      expressed portably as an ``(avg_rating IS NULL)`` ascending key (so
      ``False``/rated rows come before ``True``/unrated rows) followed by
      ``avg_rating`` descending, which behaves identically on Postgres and the
      SQLite test DB without relying on dialect-specific ``NULLS LAST``.
    """
    if sort == "oldest":
        return query.order_by(Recipe.created_at.asc())
    if sort == "most_cooked":
        return query.order_by(
            Recipe.cook_count.desc(),
            Recipe.created_at.desc(),
        )
    if sort == "highest_rated":
        return query.order_by(
            (Recipe.avg_rating.is_(None)).asc(),
            Recipe.avg_rating.desc(),
            Recipe.created_at.desc(),
        )
    # Default / explicit "newest".
    return query.order_by(Recipe.created_at.desc())


@search_bp.get("")
@require_auth
def search():
    """Filter, sort, and paginate the caller's recipes (heuristic search).

    Implements ``GET /search`` from ``docs/api.md``. Reads the repeatable
    ``ingredient``/``tag``/``tool`` params plus ``max_time``, ``q``, ``sort``,
    ``limit`` and ``offset``; validates them into a
    :class:`~app.schemas.search.SearchQuery`; builds a composable query over the
    caller's own, non-soft-deleted recipes; counts all matches for ``total``;
    then returns the requested page as recipe cards.

    Validation failures (an unrecognised ``sort``, a non-integer or negative
    ``max_time``) return 400 with a concise ``{"error": ...}`` body.

    Returns:
        JSON ``{"recipes": [<card>, ...], "total": <int>}`` with HTTP 200.
    """
    try:
        params = SearchQuery(
            ingredients=request.args.getlist("ingredient"),
            tags=request.args.getlist("tag"),
            tools=request.args.getlist("tool"),
            max_time=request.args.get("max_time"),
            q=request.args.get("q"),
            sort=request.args.get("sort"),
            limit=request.args.get("limit"),
            offset=request.args.get("offset"),
        )
    except ValidationError as error:
        return jsonify({"error": _validation_error_message(error)}), 400

    # Base scope: the caller's own, non-soft-deleted recipes (same as
    # GET /recipes).
    base_query = Recipe.query.filter(
        Recipe.user_id == current_user.id,
        Recipe.deleted_at.is_(None),
    )

    filtered = _apply_filters(base_query, params)

    # ``total`` counts every match, ignoring the page window.
    total = filtered.count()

    recipes = (
        _apply_sort(filtered, params.sort)
        .limit(params.limit)
        .offset(params.offset)
        .all()
    )

    cards = [
        RecipeCard.from_model(
            recipe,
            thumbnail_url=s3_key_to_url(recipe.thumbnail_s3_key),
            tags=[tag.name for tag in recipe.tags.all()],
        )
        for recipe in recipes
    ]

    response = RecipeListResponse(recipes=cards, total=total)
    return jsonify(response.model_dump(mode="json")), 200


# ---------------------------------------------------------------------------
# LLM natural-language search (POST /search/llm) — TASK-3.2
# ---------------------------------------------------------------------------


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
