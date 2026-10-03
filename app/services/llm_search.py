"""Natural-language recipe search powered by an LLM, with a heuristic fallback.

This service backs ``POST /search/llm`` (TASK-3.2). Given a user's free-text
query (e.g. "I have chicken thighs, garlic, and a lemon") it:

1. Builds a compact **summary index** of the caller's own, non-soft-deleted
   recipes — ``{id, title, ingredient names, tag names}`` per recipe.
2. Serves a **cached** result if one exists for this ``(user, query)`` pair.
3. Otherwise constructs a GPT prompt embedding the index + the query, asks the
   model for a STRICT-JSON ranked list of ``{recipe_id, match_explanation}``,
   parses it, maps the ids back onto the caller's recipes (dropping any id the
   model invented), preserves the model's ranking order, and builds a list of
   :class:`~app.schemas.search.LlmRecipeCard`.
4. **Caches** the LLM-derived result for 5 minutes.
5. On *any* LLM failure (exception, timeout, malformed JSON) falls back to a
   small self-contained **heuristic** token match. The fallback result is NOT
   cached (see "Caching" below).

Testability seams
-----------------
The single externally-effecting LLM step is isolated in
:func:`call_llm_rankings`. Tests monkeypatch that one function to return canned
rankings (or to raise) so the suite never hits the network. Redis access reuses
:func:`app.services.redis_client.get_redis_client`, the same monkeypatchable
seam the upload flow uses.

Caching
-------
The cache key is ``llmsearch:{user_id}:{sha256(normalized_query)}`` where the
query is normalized by stripping and lowercasing — so "Chicken " and "chicken"
hit the same entry. The cached value is the **serialized JSON response**
(``{"recipes": [...]}``) with a 300-second (5-minute) TTL, so a cache hit can be
returned verbatim without re-running the model.

Only the **LLM-derived** result is cached. The heuristic fallback result is
deliberately NOT cached: a transient LLM outage would otherwise poison the cache
with degraded results for a full 5 minutes, so that even once the model
recovered, repeat queries would keep returning the weaker heuristic answer.
Leaving the fallback uncached means the next identical request retries the LLM.

Cache writes are best-effort: if Redis is unreachable the search still succeeds
(the write/read is wrapped and a failure is logged, never raised).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from flask import current_app

from app.models.recipe import Recipe
from app.schemas.search import LlmRecipeCard, LlmSearchResponse
from app.schemas.recipe import RecipeCard
from app.services import redis_client
from app.services.urls import s3_key_to_url

logger = logging.getLogger(__name__)

#: The chat model used for ranking. GPT-4-class model with JSON-mode support.
MODEL = "gpt-4o"

#: Cache TTL for an LLM-derived result, in seconds (5 minutes).
CACHE_TTL_SECONDS = 300

#: Prefix for the Redis cache keys: ``llmsearch:<user_id>:<sha256>``.
_CACHE_KEY_PREFIX = "llmsearch:"

#: System prompt. Instructs the model to rank recipes from the supplied index
#: against the query and emit STRICT JSON only. The exact contract below is
#: what :func:`_parse_rankings` validates.
SEARCH_SYSTEM_PROMPT = """\
You are a cooking assistant that matches a user's natural-language request to
recipes from their personal collection. You are given the user's query and a
JSON array of candidate recipes, each with an "id", a "title", a list of
"ingredients" (names), and a list of "tags".

Select the recipes that best satisfy the query and rank them best-first. For
each selected recipe write a short, specific one-sentence explanation of why it
matches (reference the ingredients, tags, or theme that connect it to the
query).

Respond with STRICT JSON ONLY — no prose, no markdown code fences, no
commentary. The response MUST be a single JSON object of exactly this shape:

{
  "recipes": [
    {
      "recipe_id": string,          // an "id" copied verbatim from the input index
      "match_explanation": string   // one sentence on why it matches the query
    }
  ]
}

Rules:
- Only use "recipe_id" values that appear in the provided index. Never invent
  an id.
- Order the array best-match first.
- If nothing in the index is a reasonable match, return {"recipes": []}.
- Do not include recipes that do not plausibly match the query.
"""


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------


def build_recipe_index(user_id: object) -> list[dict]:
    """Build a compact summary index of a user's own, non-deleted recipes.

    Each entry is ``{"id", "title", "ingredients": [names], "tags": [names]}``.
    The list is ordered newest-first for a stable, deterministic prompt. Only
    the caller's recipes with ``deleted_at IS NULL`` are included.

    Args:
        user_id: The authenticated user's id.

    Returns:
        A list of per-recipe summary dicts (possibly empty).
    """
    recipes = (
        Recipe.query.filter(
            Recipe.user_id == user_id,
            Recipe.deleted_at.is_(None),
        )
        .order_by(Recipe.created_at.desc())
        .all()
    )
    return _index_from_recipes(recipes)


def _index_from_recipes(recipes: list[Recipe]) -> list[dict]:
    """Project already-loaded recipes into the compact summary index.

    Shared by :func:`build_recipe_index` and :func:`search_recipes` so the
    exact shape embedded in the prompt is defined in one place.
    """
    index: list[dict] = []
    for recipe in recipes:
        index.append(
            {
                "id": str(recipe.id),
                "title": recipe.title,
                "ingredients": [ing.name for ing in recipe.ingredients.all()],
                "tags": [tag.name for tag in recipe.tags.all()],
            }
        )
    return index


# ---------------------------------------------------------------------------
# Prompt construction + the single OpenAI-call seam
# ---------------------------------------------------------------------------


def _build_user_prompt(query: str, index: list[dict]) -> str:
    """Compose the user-turn text embedding the query and the recipe index."""
    index_json = json.dumps(index, ensure_ascii=False)
    return (
        f"User query:\n{query}\n\n"
        f"Candidate recipes (JSON array):\n{index_json}\n\n"
        "Return the ranked matches as the strict JSON object described."
    )


def call_llm_rankings(query: str, index: list[dict]) -> list[dict]:
    """Call the LLM and return its parsed ranked list of matches.

    This is the single network-touching seam; tests monkeypatch it. It builds
    the prompt, requests JSON output from the model, parses the response, and
    returns a list of ``{"recipe_id", "match_explanation"}`` dicts in the
    model's ranking order.

    Raises:
        Exception: Any OpenAI/transport error propagates to the caller
            (:func:`search_recipes`), which treats it as an LLM failure and
            falls back to the heuristic. A malformed/garbage response surfaces
            as a :class:`ValueError` from :func:`_parse_rankings`.
    """
    client = _get_openai_client()
    completion = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(query, index)},
        ],
    )
    raw = completion.choices[0].message.content or ""
    return _parse_rankings(raw)


def _get_openai_client():
    """Return an OpenAI client authenticated with the app's configured key.

    Imported lazily (mirroring ``lambda/extract_handler._get_openai_client``)
    so the module imports cleanly without the ``openai`` package being present
    and so no network/credential work happens at import time.
    """
    from openai import OpenAI  # noqa: PLC0415

    api_key = current_app.config.get("OPENAI_API_KEY") or None
    return OpenAI(api_key=api_key)


def _parse_rankings(raw: str) -> list[dict]:
    """Parse the model's STRICT-JSON response into a list of ranking dicts.

    Accepts the documented ``{"recipes": [{"recipe_id", "match_explanation"},
    ...]}`` shape. Entries missing a usable ``recipe_id`` are dropped; a missing
    ``match_explanation`` degrades to an empty string (the caller supplies a
    default). Ordering is preserved as the model returned it.

    Raises:
        ValueError: If ``raw`` is not valid JSON, is not a JSON object, or does
            not carry a ``recipes`` array. The caller treats this as an LLM
            failure and falls back to the heuristic.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError(f"LLM response was not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise ValueError("LLM response JSON was not an object")

    raw_recipes = data.get("recipes")
    if not isinstance(raw_recipes, list):
        raise ValueError("LLM response JSON missing a 'recipes' array")

    rankings: list[dict] = []
    for item in raw_recipes:
        if not isinstance(item, dict):
            continue
        recipe_id = item.get("recipe_id")
        if not isinstance(recipe_id, str) or not recipe_id.strip():
            continue
        explanation = item.get("match_explanation")
        if not isinstance(explanation, str):
            explanation = ""
        rankings.append(
            {
                "recipe_id": recipe_id.strip(),
                "match_explanation": explanation.strip(),
            }
        )
    return rankings


# ---------------------------------------------------------------------------
# Card assembly
# ---------------------------------------------------------------------------


def _default_explanation() -> str:
    """Explanation used when the model returns a match with no text."""
    return "Matches your search."


def _card_for(recipe: Recipe, match_explanation: str) -> LlmRecipeCard:
    """Build an :class:`LlmRecipeCard` for one owned recipe + explanation."""
    card = RecipeCard.from_model(
        recipe,
        thumbnail_url=s3_key_to_url(recipe.thumbnail_s3_key),
        tags=[tag.name for tag in recipe.tags.all()],
    )
    return LlmRecipeCard.from_card(
        card, match_explanation=match_explanation or _default_explanation()
    )


def _cards_from_rankings(
    rankings: list[dict], recipes_by_id: dict[str, Recipe]
) -> list[LlmRecipeCard]:
    """Map model rankings onto owned recipes, preserving order.

    Ids the model returned that are not in ``recipes_by_id`` (hallucinated or
    belonging to another user) are silently dropped. A recipe id repeated in
    the rankings is emitted once (first occurrence wins).
    """
    cards: list[LlmRecipeCard] = []
    seen: set[str] = set()
    for ranking in rankings:
        recipe_id = ranking["recipe_id"]
        if recipe_id in seen:
            continue
        recipe = recipes_by_id.get(recipe_id)
        if recipe is None:
            continue  # hallucinated / not-owned id — ignore
        seen.add(recipe_id)
        cards.append(_card_for(recipe, ranking["match_explanation"]))
    return cards


# ---------------------------------------------------------------------------
# Heuristic fallback (self-contained; does NOT depend on GET /search)
# ---------------------------------------------------------------------------

#: Tokens shorter than this (and pure stop-ish noise) are ignored when matching
#: so a query like "I have a ..." does not match on "i"/"a".
_MIN_TOKEN_LENGTH = 3

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase + split ``text`` into alphanumeric tokens >= min length."""
    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) >= _MIN_TOKEN_LENGTH
    ]


def heuristic_search(query: str, recipes: list[Recipe]) -> list[LlmRecipeCard]:
    """Rank recipes by naive token overlap with the query.

    A self-contained fallback used when the LLM path fails. Tokenizes the query
    and each recipe's searchable text (title + ingredient names + tag names),
    then keeps recipes sharing at least one token, ranked by the number of
    distinct matched tokens (descending) and then newest-first as a tiebreak.
    Each result's ``match_explanation`` names the matched tokens.

    Args:
        query: The user's natural-language query.
        recipes: The caller's own non-deleted recipes.

    Returns:
        Ranked :class:`LlmRecipeCard` list (possibly empty).
    """
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return []

    scored: list[tuple[int, Recipe, list[str]]] = []
    for recipe in recipes:
        haystack_tokens: set[str] = set(_tokenize(recipe.title or ""))
        for ing in recipe.ingredients.all():
            haystack_tokens.update(_tokenize(ing.name or ""))
        for tag in recipe.tags.all():
            haystack_tokens.update(_tokenize(tag.name or ""))

        matched = sorted(query_tokens & haystack_tokens)
        if matched:
            scored.append((len(matched), recipe, matched))

    # Rank: more matched tokens first, then newest first.
    scored.sort(key=lambda row: (-row[0], _created_sort_key(row[1])))

    cards: list[LlmRecipeCard] = []
    for _count, recipe, matched in scored:
        explanation = f"Matched on: {', '.join(matched)}"
        cards.append(_card_for(recipe, explanation))
    return cards


def _created_sort_key(recipe: Recipe):
    """Sort key placing newer recipes first within an equal token-count group.

    Returns the negative timestamp so ascending sort yields newest-first. A
    missing ``created_at`` sorts last.
    """
    created = getattr(recipe, "created_at", None)
    if created is None:
        return 0.0
    return -created.timestamp()


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def _normalize_query(query: str) -> str:
    """Normalize a query for cache keying: stripped and lowercased."""
    return query.strip().lower()


def cache_key(user_id: object, query: str) -> str:
    """Return the Redis cache key for a ``(user, query)`` pair.

    ``llmsearch:{user_id}:{sha256(normalized_query)}`` where the query is
    stripped + lowercased before hashing.
    """
    digest = hashlib.sha256(_normalize_query(query).encode("utf-8")).hexdigest()
    return f"{_CACHE_KEY_PREFIX}{user_id}:{digest}"


def _read_cache(key: str) -> dict | None:
    """Best-effort read of a cached serialized response.

    Returns the parsed ``{"recipes": [...]}`` dict on a hit, or ``None`` on a
    miss or any Redis error (logged, never raised).
    """
    try:
        client = redis_client.get_redis_client()
        raw = client.get(key)
    except Exception as error:  # noqa: BLE001 - cache read is best-effort
        logger.warning("LLM search cache read failed (continuing): %s", error)
        return None

    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # A corrupt cache entry should not break the request; treat as a miss.
        return None


def _write_cache(key: str, payload: dict) -> None:
    """Best-effort write of a serialized response with the 5-minute TTL."""
    try:
        client = redis_client.get_redis_client()
        client.set(key, json.dumps(payload), ex=CACHE_TTL_SECONDS)
    except Exception as error:  # noqa: BLE001 - cache write is best-effort
        logger.warning("LLM search cache write failed (continuing): %s", error)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def search_recipes(user_id: object, query: str) -> dict:
    """Run a natural-language search and return the serialized response dict.

    Full flow (see module docstring):

    1. Serve from cache if present.
    2. Build the caller's recipe index; short-circuit to ``{"recipes": []}``
       (without calling the LLM) if the user has no recipes.
    3. Call the LLM, map rankings onto owned recipes, cache, and return.
    4. On any LLM failure, fall back to the heuristic (result NOT cached).

    Args:
        user_id: The authenticated user's id.
        query: The validated natural-language query.

    Returns:
        A ``{"recipes": [...]}`` dict ready to ``jsonify``. Each card carries a
        ``match_explanation``.
    """
    key = cache_key(user_id, query)

    cached = _read_cache(key)
    if cached is not None:
        return cached

    recipes = (
        Recipe.query.filter(
            Recipe.user_id == user_id,
            Recipe.deleted_at.is_(None),
        )
        .order_by(Recipe.created_at.desc())
        .all()
    )

    # No recipes at all — there is nothing for the model to rank, so skip the
    # LLM entirely and return an empty result.
    if not recipes:
        return LlmSearchResponse(recipes=[]).model_dump(mode="json")

    recipes_by_id = {str(recipe.id): recipe for recipe in recipes}
    index = _index_from_recipes(recipes)

    try:
        rankings = call_llm_rankings(query, index)
        cards = _cards_from_rankings(rankings, recipes_by_id)
    except Exception as error:  # noqa: BLE001 - any LLM failure -> fallback
        logger.warning(
            "LLM search failed for user %s; falling back to heuristic: %s",
            user_id,
            error,
        )
        cards = heuristic_search(query, recipes)
        # Fallback result is intentionally NOT cached (see module docstring).
        return LlmSearchResponse(recipes=cards).model_dump(mode="json")

    payload = LlmSearchResponse(recipes=cards).model_dump(mode="json")
    _write_cache(key, payload)
    return payload
