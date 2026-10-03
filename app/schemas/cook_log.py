"""Pydantic v2 schemas for the cook log endpoints (TASK-4.1).

Covers the request/response shapes for the recipe-scoped cook log surface
documented in ``docs/api.md`` under *Cook Logs*
(``/v1/recipes/{id}/cooks``):

* :class:`CookLogCreate` — the ``POST`` body: ``cooked_at`` required,
  ``notes``/``rating``/``photo_s3_key`` optional, ``rating`` constrained to
  1..5.
* :class:`CookLogUpdate` — the ``PATCH`` body: ``notes``/``rating``/
  ``photo_s3_key`` all optional. ``cooked_at`` is intentionally **not** a field
  here (``docs/api.md`` says PATCH updates "notes, rating, or photo"), so a
  ``cooked_at`` in the body is rejected as an unknown field (``extra="forbid"``
  → 400).
* :class:`CookLogResponse` — a single cook log object as returned by GET/POST/
  PATCH.
* :class:`CookLogListResponse` — the envelope ``{"cooks": [...]}``.

Photo handling
--------------
A cook log carries a ``photo_s3_key`` the client obtains via the **existing**
``POST /recipes/upload`` presigned-URL flow — the same endpoint/service recipe
images use. There is no separate cook-log upload endpoint: the client uploads
the bytes straight to S3, then hands the resulting key back on create/update.
The public ``photo_url`` is **derived** from that key at serialization time via
:func:`app.services.urls.s3_key_to_url` (never persisted), mirroring how recipe
image/thumbnail URLs are resolved.
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field


class CookLogCreate(BaseModel):
    """Validated request body for ``POST /recipes/{id}/cooks``.

    Only ``cooked_at`` is required (an ISO ``YYYY-MM-DD`` date); ``notes``,
    ``rating``, and ``photo_s3_key`` are optional. ``rating`` is constrained to
    the inclusive range 1..5 so a 0 or 6 is rejected with a 400 (mirroring the
    model's ``ck_cook_logs_rating`` CHECK constraint). Unknown fields are
    rejected (``extra="forbid"``) so a typo surfaces as a 400 rather than being
    silently dropped.

    ``photo_s3_key`` is the key returned by the shared ``POST /recipes/upload``
    presigned flow; it is wired straight through to the stored column and the
    response's derived ``photo_url``.
    """

    model_config = ConfigDict(extra="forbid")

    cooked_at: datetime.date
    notes: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    photo_s3_key: str | None = None


class CookLogUpdate(BaseModel):
    """Validated request body for ``PATCH /recipes/{id}/cooks/{cook_id}``.

    Per ``docs/api.md`` the PATCH endpoint updates "notes, rating, or photo",
    so this model exposes exactly those three fields and every one is optional.
    ``cooked_at`` is deliberately absent: because ``extra="forbid"`` rejects
    unknown fields, a client that sends ``cooked_at`` gets a 400 rather than
    silently having it ignored — the cook date is fixed at creation time.

    Omitted vs. explicit null
    -------------------------
    The view applies only the fields the client actually sent, via
    ``model_dump(exclude_unset=True)``. This distinguishes an omitted field
    (leave the stored value untouched) from one explicitly set to ``null``
    (clear it). All three columns are nullable, so each may be cleared by
    sending ``null`` — e.g. ``{"rating": null}`` removes the rating, which then
    drops the log out of the parent recipe's ``avg_rating`` recalculation.

    ``rating``, when provided (and non-null), is constrained to 1..5.
    """

    model_config = ConfigDict(extra="forbid")

    notes: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    photo_s3_key: str | None = None


class CookLogResponse(BaseModel):
    """A single cook log object, as returned by GET/POST/PATCH.

    Mirrors the shape in ``docs/api.md``: ``{id, cooked_at, notes, rating,
    photo_url, created_at}``. ``photo_url`` is derived from the stored
    ``photo_s3_key`` by the view (via :func:`app.services.urls.s3_key_to_url`)
    and is ``null`` when the log has no photo.
    """

    id: uuid.UUID
    cooked_at: datetime.date
    notes: str | None = None
    rating: int | None = None
    photo_url: str | None = None
    created_at: datetime.datetime

    @classmethod
    def from_model(cls, cook_log: object, *, photo_url: str | None) -> "CookLogResponse":
        """Build a response from a :class:`~app.models.cook_log.CookLog`.

        Args:
            cook_log: The ORM cook log record.
            photo_url: The pre-resolved public URL derived from
                ``cook_log.photo_s3_key`` (``None`` when there is no photo).

        Returns:
            A populated :class:`CookLogResponse`.
        """
        return cls(
            id=cook_log.id,
            cooked_at=cook_log.cooked_at,
            notes=cook_log.notes,
            rating=cook_log.rating,
            photo_url=photo_url,
            created_at=cook_log.created_at,
        )


class CookLogListResponse(BaseModel):
    """Envelope for ``GET /recipes/{id}/cooks``: ``{"cooks": [...]}``.

    Matches the ``{"recipes": [...]}`` / ``{"tags": [...]}`` envelopes used
    elsewhere in the API rather than a bare top-level list.
    """

    cooks: list[CookLogResponse]
