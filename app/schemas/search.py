"""Pydantic v2 schema for the ``GET /search`` heuristic filter endpoint.

This module hosts :class:`SearchQuery`, the validated/normalised form of the
query parameters documented for ``GET /search`` in ``docs/api.md``. The search
*response* reuses the recipe-card shapes from :mod:`app.schemas.recipe`
(``RecipeCard`` / ``RecipeListResponse``) because the card is identical to the
one returned by ``GET /recipes`` — only the filtering differs.

Validation choices (documented inline on the fields):

* ``sort`` is a closed enum; an unrecognised value is a 400 (it is a typed
  control, not free text, so silently defaulting would hide a client bug).
* ``max_time`` must be a non-negative integer; a non-integer or negative value
  is a 400 for the same reason — it is a typed numeric filter, not a knob we
  want to silently clamp into a different query than the client asked for.
* ``limit``/``offset`` reuse the *clamping* behaviour of
  :class:`~app.schemas.recipe.RecipeListQuery` (default 20, clamp to 1..100,
  floor offset at 0) so paging degrades gracefully and matches ``GET /recipes``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Reuse the list endpoint's pagination defaults/bounds so the two endpoints
# page identically.
from app.schemas.recipe import DEFAULT_LIMIT, MAX_LIMIT

#: The four accepted ``sort`` values, in the order documented in
#: ``docs/api.md``. ``newest`` is the default when the param is absent.
SortOption = Literal["newest", "oldest", "most_cooked", "highest_rated"]

DEFAULT_SORT: SortOption = "newest"


class SearchQuery(BaseModel):
    """Validated, normalised parameters for ``GET /search``.

    Every parameter is optional and the filters AND together (both across
    different fields and, for the repeatable fields, across the values within a
    single field — see the multi-value note below).

    Multi-value (ALL) semantics
    ---------------------------
    ``ingredients``, ``tags`` and ``tools`` are repeatable query params. A
    recipe matches only if it contains **ALL** listed values (logical AND),
    e.g. ``?ingredient=chicken&ingredient=garlic`` returns recipes that have
    *both* chicken and garlic. This reconciles the two phrasings in the task:
    the backlog line mentions "OR across values" but immediately clarifies
    "multiple ingredients = recipes containing ALL of them", and both
    ``docs/api.md`` and the done-criteria specify ALL — so ALL/AND is what we
    implement here and in the query builder.

    Name matching for these three filters is case-insensitive exact match on
    the (stripped) name — not a substring — so ``?tag=italian`` matches the
    ``Italian`` tag but not a ``Italian-American`` tag. ``q`` is the one
    substring filter (case-insensitive, against the title).

    Normalisation
    -------------
    Each repeatable value is stripped of surrounding whitespace and blank
    entries are dropped, so a stray ``?ingredient=`` does not force an
    impossible "contains the empty-named ingredient" filter. ``q`` is stripped
    and, if it collapses to empty, treated as absent.
    """

    model_config = ConfigDict(extra="ignore")

    ingredients: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    max_time: int | None = Field(default=None, ge=0)
    q: str | None = None
    sort: SortOption = DEFAULT_SORT
    limit: int = Field(default=DEFAULT_LIMIT)
    offset: int = Field(default=0)

    # -- repeatable string lists: strip + drop blanks ----------------------

    @field_validator("ingredients", "tags", "tools", mode="before")
    @classmethod
    def _clean_list(cls, value: object) -> object:
        """Normalise a repeatable param to a list of non-blank, stripped names.

        Accepts either ``None`` (param absent), a single string, or a list of
        strings (Flask ``request.args.getlist``). Surrounding whitespace is
        stripped from each value and empty results are dropped.
        """
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        cleaned: list[str] = []
        for item in value:
            if item is None:
                continue
            stripped = str(item).strip()
            if stripped:
                cleaned.append(stripped)
        return cleaned

    # -- q: strip, empty -> absent ----------------------------------------

    @field_validator("q", mode="before")
    @classmethod
    def _clean_q(cls, value: object) -> object:
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

    # -- max_time: blank -> absent (non-int/negative stay a 400) ----------

    @field_validator("max_time", mode="before")
    @classmethod
    def _default_max_time(cls, value: object) -> object:
        # An absent or blank ``max_time`` means "no time filter". A present but
        # non-integer or negative value is left to fail validation (-> 400).
        if value is None or value == "":
            return None
        return value

    # -- sort: blank -> default (bad value stays a 400) -------------------

    @field_validator("sort", mode="before")
    @classmethod
    def _default_sort(cls, value: object) -> object:
        if value is None or value == "":
            return DEFAULT_SORT
        return value

    # -- limit/offset: clamp, matching RecipeListQuery --------------------

    @field_validator("limit", mode="before")
    @classmethod
    def _default_limit(cls, value: object) -> object:
        if value is None or value == "":
            return DEFAULT_LIMIT
        return value

    @field_validator("offset", mode="before")
    @classmethod
    def _default_offset(cls, value: object) -> object:
        if value is None or value == "":
            return 0
        return value

    @field_validator("limit")
    @classmethod
    def _clamp_limit(cls, value: int) -> int:
        if value < 1:
            return 1
        if value > MAX_LIMIT:
            return MAX_LIMIT
        return value

    @field_validator("offset")
    @classmethod
    def _clamp_offset(cls, value: int) -> int:
        return max(value, 0)
