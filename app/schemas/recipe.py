"""Pydantic v2 schemas for the recipe endpoints.

This module hosts the request/response shapes for the recipe resource. It
currently covers the ``GET /recipes`` list endpoint:

* :class:`RecipeListQuery` — validates and clamps the pagination query params.
* :class:`RecipeCard` — the compact "card" projection of a recipe used in list
  and search responses.
* :class:`RecipeListResponse` — the envelope ``{"recipes": [...], "total": N}``.

It also covers the ``POST /recipes`` create endpoint, the ``PATCH /recipes/{id}``
partial-update endpoint, and the full recipe object returned by
``POST``/``GET {id}``/``PATCH {id}``:

* :class:`RecipeCreate` — validates the manual-entry create body.
* :class:`RecipeUpdate` — validates the partial-update body (every field
  optional).
* :class:`RecipeDetail` — the complete recipe object (scalars + nested
  ingredients/tools/instructions/tags + cook summary).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Pagination query
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class RecipeListQuery(BaseModel):
    """Validated pagination parameters for ``GET /recipes``.

    ``limit`` defaults to 20 and is clamped to the inclusive range ``1..100``;
    ``offset`` defaults to 0 and is floored at 0. Values are clamped rather
    than rejected so that over-large or negative inputs degrade gracefully to
    the nearest valid value instead of returning a 400.
    """

    limit: int = Field(default=DEFAULT_LIMIT)
    offset: int = Field(default=0)

    @field_validator("limit", mode="before")
    @classmethod
    def _default_limit(cls, value: object) -> object:
        # Treat an absent/blank query param as "use the default".
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


# ---------------------------------------------------------------------------
# Recipe card (list / search item)
# ---------------------------------------------------------------------------


class RecipeCard(BaseModel):
    """Compact projection of a recipe for list and search responses.

    This is the shape documented for each item in ``GET /recipes`` and
    ``GET /search``. ``thumbnail_url`` is derived from the recipe's stored
    thumbnail S3 key (not a column) and ``tags`` is the list of tag name
    strings attached to the recipe.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    thumbnail_url: str | None = None
    total_time_min: int | None = None
    cook_count: int | None = None
    avg_rating: float | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime.datetime

    @classmethod
    def from_model(
        cls,
        recipe: object,
        *,
        thumbnail_url: str | None,
        tags: list[str],
    ) -> "RecipeCard":
        """Build a card from a :class:`~app.models.recipe.Recipe` instance.

        Args:
            recipe: The ORM recipe record.
            thumbnail_url: Pre-resolved public URL for the thumbnail (derived
                from ``recipe.thumbnail_s3_key`` by the caller), or ``None``.
            tags: The recipe's tag names as plain strings.

        Returns:
            A populated :class:`RecipeCard`.
        """
        return cls(
            id=recipe.id,
            title=recipe.title,
            thumbnail_url=thumbnail_url,
            total_time_min=recipe.total_time_min,
            cook_count=recipe.cook_count,
            avg_rating=recipe.avg_rating,
            tags=tags,
            created_at=recipe.created_at,
        )


# ---------------------------------------------------------------------------
# List response envelope
# ---------------------------------------------------------------------------


class RecipeListResponse(BaseModel):
    """Envelope for ``GET /recipes``: the page of cards plus the total count.

    ``total`` is the count of all matching recipes (ignoring pagination), so
    clients can compute how many pages exist.
    """

    recipes: list[RecipeCard]
    total: int


# ---------------------------------------------------------------------------
# Image upload (presigned URL request/response)
# ---------------------------------------------------------------------------

#: The MIME types accepted for a direct-to-S3 image/document upload, mirroring
#: the "Supported types" line in ``docs/api.md`` under ``POST /recipes/upload``.
#: A ``content_type`` outside this set is rejected with a 400.
ALLOWED_UPLOAD_CONTENT_TYPES = (
    "image/jpeg",
    "image/png",
    "image/heic",
    "application/pdf",
)

#: ``Literal`` form of :data:`ALLOWED_UPLOAD_CONTENT_TYPES` so pydantic rejects
#: an unsupported ``content_type`` as a validation error (surfaced as a 400).
UploadContentType = Literal[
    "image/jpeg",
    "image/png",
    "image/heic",
    "application/pdf",
]


class UploadRequest(BaseModel):
    """Validated request body for ``POST /recipes/upload``.

    Mirrors ``docs/api.md``: the client declares the ``filename`` it wants to
    upload and the ``content_type`` of the bytes. ``filename`` must be a
    non-empty string (after stripping); ``content_type`` is constrained to the
    four supported MIME types, so an unsupported type is rejected with a 400
    rather than producing an unusable presigned URL. Unknown fields are
    rejected (``extra="forbid"``) so a typo surfaces as a 400 instead of being
    silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    filename: str
    content_type: UploadContentType

    @field_validator("filename")
    @classmethod
    def _filename_non_empty(cls, value: str) -> str:
        """Require a filename that is non-empty after stripping whitespace."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("filename must not be empty")
        return stripped


class UploadResponse(BaseModel):
    """Response body for ``POST /recipes/upload``.

    Carries the presigned ``upload_url`` the client ``PUT``s the bytes to and
    the ``s3_key`` the object will live at — the key the client later hands to
    ``POST /recipes/extract``. The URL is time-limited (15 minutes); see
    :func:`app.services.s3.generate_presigned_put`.
    """

    upload_url: str
    s3_key: str

# ---------------------------------------------------------------------------
# Create request
# ---------------------------------------------------------------------------

#: The five recipe origins accepted by the API (mirrors the ``ck_recipes_origin``
#: CHECK constraint on the model).
RecipeOrigin = Literal["instagram", "web", "cookbook", "manual", "ios_share"]


class RecipeCreate(BaseModel):
    """Validated request body for ``POST /recipes`` (manual entry).

    Only ``title`` is required; every other field is optional and left as
    ``None`` when omitted. Unknown fields are rejected (``extra="forbid"``) so
    that typos in field names surface as a 400 rather than being silently
    dropped. ``origin`` is constrained to the five allowed values; the time and
    serving counts must be non-negative.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str | None = None
    prep_time_min: int | None = Field(default=None, ge=0)
    cook_time_min: int | None = Field(default=None, ge=0)
    total_time_min: int | None = Field(default=None, ge=0)
    servings: int | None = Field(default=None, ge=0)
    origin: RecipeOrigin | None = None
    source_url: str | None = None
    source_citation: str | None = None

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, value: str) -> str:
        """Require a title that is non-empty after stripping whitespace."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be empty")
        return stripped


# ---------------------------------------------------------------------------
# Update request (PATCH {id})
# ---------------------------------------------------------------------------


class RecipeUpdate(BaseModel):
    """Validated request body for ``PATCH /recipes/{id}`` (partial update).

    Mirrors :class:`RecipeCreate`'s field set, but *every* field is optional —
    including ``title`` — so a client can send only the fields it wants to
    change. As with create, unknown fields are rejected (``extra="forbid"``),
    ``origin`` is constrained to the five allowed values, and the time/serving
    counts must be non-negative.

    Omitted vs. explicit null
    -------------------------
    The view applies only the fields the client actually sent, using
    ``model_dump(exclude_unset=True)``. This distinguishes "field omitted"
    (leave the stored value untouched) from "field set to ``null``" (clear the
    stored value). Explicitly sending ``null`` is allowed for every nullable
    field — ``description``, the time/serving counts, ``origin``,
    ``source_url``, and ``source_citation`` — and clears it on the recipe.

    ``title`` is the one exception: it is non-nullable on the model, so a
    provided ``title`` must be a non-empty string after stripping. Both an
    explicit ``null`` and an empty/whitespace-only string are rejected with a
    validation error; omitting ``title`` entirely leaves it unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    prep_time_min: int | None = Field(default=None, ge=0)
    cook_time_min: int | None = Field(default=None, ge=0)
    total_time_min: int | None = Field(default=None, ge=0)
    servings: int | None = Field(default=None, ge=0)
    origin: RecipeOrigin | None = None
    source_url: str | None = None
    source_citation: str | None = None

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, value: str | None) -> str | None:
        """Reject a provided ``title`` that is null or empty after stripping.

        ``None`` here means the client sent ``"title": null`` (pydantic has
        already parsed it); because the title column is non-nullable we treat
        that the same as an empty string and reject it. An omitted ``title``
        never reaches this validator.
        """
        if value is None:
            raise ValueError("title must not be null")
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be empty")
        return stripped


# ---------------------------------------------------------------------------
# Full recipe detail (POST / GET {id} response)
# ---------------------------------------------------------------------------


class IngredientDetail(BaseModel):
    """One ingredient line in a :class:`RecipeDetail`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    quantity: float | None = None
    unit: str | None = None
    preparation: str | None = None
    sort_order: int


class ToolDetail(BaseModel):
    """One required tool in a :class:`RecipeDetail`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sort_order: int


class InstructionDetail(BaseModel):
    """One ordered cooking step in a :class:`RecipeDetail`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_number: int
    body: str


class CookSummary(BaseModel):
    """Denormalised cook stats attached to a :class:`RecipeDetail`."""

    cook_count: int
    avg_rating: float | None = None


class RecipeDetail(BaseModel):
    """The complete recipe object returned by ``POST`` and ``GET /{id}``.

    Bundles the recipe's scalar columns with its nested ingredients, tools,
    instructions, and tag names, plus a ``cook_summary`` echoing the
    denormalised ``cook_count``/``avg_rating``. ``image_url``/``thumbnail_url``
    are derived from the stored S3 keys rather than being columns. For a
    freshly created manual recipe the nested lists are empty.
    """

    id: uuid.UUID
    title: str
    description: str | None = None
    prep_time_min: int | None = None
    cook_time_min: int | None = None
    total_time_min: int | None = None
    servings: int | None = None
    origin: str | None = None
    source_url: str | None = None
    source_citation: str | None = None
    image_url: str | None = None
    thumbnail_url: str | None = None
    extraction_status: str | None = None
    cook_count: int | None = None
    avg_rating: float | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    ingredients: list[IngredientDetail] = Field(default_factory=list)
    tools: list[ToolDetail] = Field(default_factory=list)
    instructions: list[InstructionDetail] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    cook_summary: CookSummary

    @classmethod
    def from_model(cls, recipe: object) -> "RecipeDetail":
        """Assemble a full detail object from a ``Recipe`` ORM instance.

        Resolves ``image_url``/``thumbnail_url`` from the recipe's stored S3
        keys and materialises the (lazy, ordered) relationship collections.

        Args:
            recipe: The ORM recipe record, with its ``ingredients``,
                ``tools``, ``instructions``, and ``tags`` relationships
                reachable.

        Returns:
            A populated :class:`RecipeDetail`.
        """
        # Imported here (not at module top) to avoid a schema→service import
        # cycle and to keep the Flask ``current_app`` dependency request-scoped.
        from app.services.urls import s3_key_to_url  # noqa: PLC0415

        return cls(
            id=recipe.id,
            title=recipe.title,
            description=recipe.description,
            prep_time_min=recipe.prep_time_min,
            cook_time_min=recipe.cook_time_min,
            total_time_min=recipe.total_time_min,
            servings=recipe.servings,
            origin=recipe.origin,
            source_url=recipe.source_url,
            source_citation=recipe.source_citation,
            image_url=s3_key_to_url(recipe.image_s3_key),
            thumbnail_url=s3_key_to_url(recipe.thumbnail_s3_key),
            extraction_status=recipe.extraction_status,
            cook_count=recipe.cook_count,
            avg_rating=recipe.avg_rating,
            created_at=recipe.created_at,
            updated_at=recipe.updated_at,
            ingredients=[
                IngredientDetail.model_validate(ingredient)
                for ingredient in recipe.ingredients
            ],
            tools=[ToolDetail.model_validate(tool) for tool in recipe.tools],
            instructions=[
                InstructionDetail.model_validate(instruction)
                for instruction in recipe.instructions
            ],
            tags=[tag.name for tag in recipe.tags],
            cook_summary=CookSummary(
                cook_count=recipe.cook_count or 0,
                avg_rating=recipe.avg_rating,
            ),
        )
