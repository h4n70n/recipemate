"""Pydantic v2 schemas for the search endpoints.

This module hosts the request/response shapes for ``POST /search/llm`` — the
AI-powered natural-language search (TASK-3.2). The heuristic filter endpoint
(``GET /search``, TASK-3.1) lives on a separate branch and is intentionally not
referenced here.

* :class:`LlmSearchRequest` — validates the ``{"query": "..."}`` body.
* :class:`LlmRecipeCard` — a :class:`~app.schemas.recipe.RecipeCard` plus a
  ``match_explanation`` string describing why the recipe matched the query.
* :class:`LlmSearchResponse` — the envelope ``{"recipes": [...]}``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.recipe import RecipeCard


#: Upper bound on a natural-language query's length (after stripping). A
#: generous-but-finite cap keeps a stray paste from building an unbounded LLM
#: prompt (and an unbounded cache key payload). Enforced as a 400.
MAX_QUERY_LENGTH = 1000


class LlmSearchRequest(BaseModel):
    """Validated request body for ``POST /search/llm``.

    The caller supplies a single natural-language ``query`` (e.g. "I have
    chicken thighs, garlic, and a lemon"). It must be a non-empty string after
    stripping surrounding whitespace and no longer than
    :data:`MAX_QUERY_LENGTH` characters. Unknown fields are rejected
    (``extra="forbid"``) so a typo surfaces as a 400 rather than being silently
    dropped.
    """

    model_config = ConfigDict(extra="forbid")

    query: str

    @field_validator("query")
    @classmethod
    def _query_valid(cls, value: str) -> str:
        """Strip the query and require it be non-empty and within the cap."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be empty")
        if len(stripped) > MAX_QUERY_LENGTH:
            raise ValueError(
                f"query must be at most {MAX_QUERY_LENGTH} characters"
            )
        return stripped


class LlmRecipeCard(RecipeCard):
    """A recipe card augmented with an LLM (or fallback) match explanation.

    Extends :class:`~app.schemas.recipe.RecipeCard` with a single additional
    field, ``match_explanation``: a short human-readable sentence describing
    why this recipe matched the user's natural-language query. For LLM results
    the explanation comes from the model; for the heuristic fallback it is a
    generic "Matched on: <tokens>" string.
    """

    match_explanation: str

    @classmethod
    def from_card(
        cls, card: RecipeCard, *, match_explanation: str
    ) -> "LlmRecipeCard":
        """Promote a plain :class:`RecipeCard` to an :class:`LlmRecipeCard`.

        Copies the card's fields verbatim and attaches ``match_explanation``.

        Args:
            card: The already-built compact recipe card.
            match_explanation: The reason this recipe matched the query.

        Returns:
            A populated :class:`LlmRecipeCard`.
        """
        return cls(**card.model_dump(), match_explanation=match_explanation)


class LlmSearchResponse(BaseModel):
    """Envelope for ``POST /search/llm``: the ranked list of matching cards.

    Mirrors the ``{"recipes": [...]}`` shape used across the API. The order of
    ``recipes`` is significant — it is the ranking (best match first) produced
    by the LLM, or the heuristic ordering when the fallback path is taken.
    """

    recipes: list[LlmRecipeCard]
