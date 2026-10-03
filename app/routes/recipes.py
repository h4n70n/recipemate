"""Recipe resource endpoints.

Houses the recipe CRUD surface documented in ``docs/api.md`` under
``/v1/recipes``. Currently implements ``GET /recipes`` (paginated list of the
authenticated user's recipes); the remaining CRUD endpoints are added by their
respective tasks.
"""

from __future__ import annotations

import datetime
import logging

from flask import Blueprint, jsonify, request
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app import db
from app.auth import AuthError, current_user, require_auth
from app.models.recipe import Recipe, RecipeTag, Tag
from app.schemas.recipe import (
    ExtractionStatusResponse,
    ExtractRequest,
    ExtractResponse,
    RecipeCard,
    RecipeCreate,
    RecipeDetail,
    RecipeListQuery,
    RecipeListResponse,
    RecipeUpdate,
    TagAddRequest,
    TagResponse,
    UploadRequest,
    UploadResponse,
)
from app.services import redis_client, s3, sqs
from app.services.urls import s3_key_to_url

recipes_bp = Blueprint("recipes", __name__, url_prefix="/v1/recipes")

logger = logging.getLogger(__name__)

#: Placeholder title for a recipe created by ``POST /recipes/extract`` before
#: the async consumer knows the real title. ``recipes.title`` is NOT NULL, but
#: the recipe is created *before* extraction runs, so we seed a human-readable
#: placeholder; the extraction Lambda overwrites it when it completes.
_EXTRACTION_PLACEHOLDER_TITLE = "Untitled recipe"


def _validation_error_message(error: ValidationError) -> str:
    """Flatten a pydantic ``ValidationError`` into one concise 400 message.

    Reports the first problem as ``"<field>: <reason>"`` (or just the reason
    for a model-level error), which keeps the ``{"error": ...}`` body short and
    human-readable instead of echoing pydantic's full structured error list.
    Shared by the create and (future) update views so both surface validation
    failures the same way.

    Args:
        error: The raised pydantic validation error.

    Returns:
        A single-line error message suitable for ``{"error": <message>}``.
    """
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = first.get("msg", "invalid request")
    return f"{location}: {message}" if location else message


def _get_owned_recipe_or_error(recipe_id):
    """Look up a recipe the caller owns, or report the right error.

    Shared by :func:`get_recipe` and :func:`update_recipe` so both apply the
    identical lookup/ownership rules documented in ``docs/api.md``:

    * a missing or soft-deleted recipe is reported as 404,
    * a recipe owned by a different user raises :class:`AuthError` (403) — the
      documented "authenticated but not the resource owner" case (which, as
      noted on :func:`get_recipe`, confirms existence to a non-owner; we follow
      the documented contract rather than a 404-everywhere privacy policy).

    Args:
        recipe_id: The recipe UUID parsed from the path by Flask's ``uuid``
            converter.

    Returns:
        A ``(recipe, error_response)`` pair. On success ``recipe`` is the ORM
        record and ``error_response`` is ``None``. For a missing/soft-deleted
        recipe ``recipe`` is ``None`` and ``error_response`` is the ``(json,
        404)`` tuple the view should return as-is. A non-owner never returns —
        it raises :class:`AuthError` (403).
    """
    recipe = Recipe.query.get(recipe_id)

    # Treat a soft-deleted recipe as if it does not exist.
    if recipe is None or recipe.deleted_at is not None:
        return None, (jsonify({"error": "Recipe not found"}), 404)

    if recipe.user_id != current_user.id:
        raise AuthError("You do not have access to this recipe", 403)

    return recipe, None


@recipes_bp.get("")
@require_auth
def list_recipes():
    """List the authenticated user's recipes with pagination.

    Returns the caller's own, non-soft-deleted recipes as compact cards,
    newest first, paginated by ``limit``/``offset`` query params. ``total`` is
    the full count of matching recipes irrespective of the page window.

    Query params:
        limit: Page size (default 20, clamped to 1..100).
        offset: Number of records to skip (default 0, floored at 0).

    Returns:
        JSON ``{"recipes": [<card>, ...], "total": <int>}`` with HTTP 200.
    """
    query_params = RecipeListQuery(
        limit=request.args.get("limit"),
        offset=request.args.get("offset"),
    )

    # Base query: the caller's recipes, excluding soft-deleted rows.
    base_query = Recipe.query.filter(
        Recipe.user_id == current_user.id,
        Recipe.deleted_at.is_(None),
    )

    total = base_query.count()

    recipes = (
        base_query.order_by(Recipe.created_at.desc())
        .limit(query_params.limit)
        .offset(query_params.offset)
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


@recipes_bp.post("")
@require_auth
def create_recipe():
    """Create a recipe from manually entered fields.

    Accepts the manual-entry create body documented in ``docs/api.md`` under
    ``POST /recipes`` — ``title`` is required, everything else optional, and no
    image is involved. The new recipe is owned by the authenticated caller and
    created with no extraction (``extraction_status`` left ``None``) and a zero
    cook count. Nested ingredients/tools/instructions/tags start empty.

    Validation errors (missing/empty ``title``, bad ``origin``, negative
    times/servings, unknown fields) are rejected with HTTP 400 and a concise
    ``{"error": <message>}`` body. Validation is caught in-view rather than via
    a global handler so the message stays specific and so the same helper can
    be reused by the update view.

    Returns:
        JSON of the full :class:`RecipeDetail` object with HTTP 201.
    """
    try:
        payload = RecipeCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as error:
        return jsonify({"error": _validation_error_message(error)}), 400

    recipe = Recipe(
        user_id=current_user.id,
        title=payload.title,
        description=payload.description,
        prep_time_min=payload.prep_time_min,
        cook_time_min=payload.cook_time_min,
        total_time_min=payload.total_time_min,
        servings=payload.servings,
        origin=payload.origin,
        source_url=payload.source_url,
        source_citation=payload.source_citation,
        extraction_status=None,
        cook_count=0,
    )
    db.session.add(recipe)
    db.session.commit()

    detail = RecipeDetail.from_model(recipe)
    return jsonify(detail.model_dump(mode="json")), 201


#: Presigned upload URL (and the matching metadata record) live for 15 minutes,
#: as documented in ``docs/api.md`` under ``POST /recipes/upload``.
_UPLOAD_URL_TTL_SECONDS = 900


@recipes_bp.post("/upload")
@require_auth
def upload():
    """Mint a presigned S3 ``PUT`` URL for a direct-to-S3 image/document upload.

    Implements ``POST /recipes/upload`` from ``docs/api.md``. The caller
    declares the ``filename`` and ``content_type`` it wants to upload; the view
    validates them, allocates an opaque, collision-free object key
    (``uploads/<uuid>/<basename>`` via
    :func:`~app.services.s3.build_upload_key`, which also sanitises the
    filename to its basename), and returns a presigned ``PUT`` URL valid for 15
    minutes together with that key. The client then ``PUT``s the bytes straight
    to S3 and later hands the ``s3_key`` to ``POST /recipes/extract``.

    Validation failures — an unsupported ``content_type`` (only
    ``image/jpeg``, ``image/png``, ``image/heic``, ``application/pdf`` are
    accepted), a missing/empty ``filename``, or any unknown field — return 400
    with the shared concise ``{"error": ...}`` body.

    Upload metadata (requesting user, filename, content type) is recorded in
    Redis keyed by the S3 key on a **best-effort** basis: if Redis is
    unavailable the write is skipped (logged as a warning) and the presigned
    URL is still returned, since the URL — not the cache record — is the
    product of this endpoint. See
    :func:`~app.services.redis_client.store_upload_metadata`.

    Returns:
        JSON ``{"upload_url": "https://...", "s3_key": "uploads/<uuid>/..."}``
        with HTTP 200.
    """
    try:
        payload = UploadRequest.model_validate(request.get_json(silent=True) or {})
    except ValidationError as error:
        return jsonify({"error": _validation_error_message(error)}), 400

    s3_key = s3.build_upload_key(current_user.id, payload.filename)
    upload_url = s3.generate_presigned_put(
        s3_key,
        payload.content_type,
        expires_in=_UPLOAD_URL_TTL_SECONDS,
    )

    # Best-effort: a Redis outage here must not fail the request.
    redis_client.store_upload_metadata(
        s3_key,
        current_user.id,
        payload.filename,
        payload.content_type,
        ttl=_UPLOAD_URL_TTL_SECONDS,
    )

    response = UploadResponse(upload_url=upload_url, s3_key=s3_key)
    return jsonify(response.model_dump(mode="json")), 200


@recipes_bp.post("/extract")
@require_auth
def extract_recipe():
    """Create a pending recipe and enqueue async extraction from an upload.

    Implements ``POST /recipes/extract`` from ``docs/api.md``. The caller hands
    back the ``s3_key`` of an object it already uploaded (via
    ``POST /recipes/upload``) together with an optional ``origin`` and
    ``source_url``. The view:

    1. validates the body (400 on a missing/empty ``s3_key``, bad ``origin``,
       or unknown field),
    2. creates a recipe owned by the caller with ``extraction_status="pending"``
       and a placeholder ``title`` (``recipes.title`` is NOT NULL but the real
       title is not known until extraction finishes; the Lambda overwrites it),
    3. commits, then enqueues an SQS message carrying the new recipe id, the
       ``s3_key``, and the owner id for the extraction consumer,
    4. returns ``{"recipe_id", "extraction_status": "pending"}`` with 201.

    Enqueue-failure contract
    ------------------------
    If the SQS send fails, the recipe is **rolled back** (deleted) and the
    request returns 503. This is the cleaner contract: a ``pending`` recipe
    whose job never reached the queue would be a permanent orphan the consumer
    never processes, so we would rather fail the call atomically and let the
    client retry than leave a stuck row behind. (The alternative — persist the
    recipe as ``failed`` and still return 201 — was rejected because it leaves
    a dead recipe in the user's list for an outage that is almost always
    transient and retryable.)

    Upload-attribution note
    -----------------------
    We do **not** cross-check the Redis ``upload:<s3_key>`` metadata against the
    caller: that record is best-effort and expires with the presigned URL (15
    min), so a legitimate extract shortly after the window closes would fail
    spuriously. Ownership is instead established by making the authenticated
    caller the recipe owner. The uploaded key is opaque/unguessable (a uuid
    segment), so this is an acceptable trade-off.

    Returns:
        JSON ``{"recipe_id": "<uuid>", "extraction_status": "pending"}`` with
        HTTP 201, or ``{"error": ...}`` with 400 (validation) / 503 (enqueue
        failure).
    """
    try:
        payload = ExtractRequest.model_validate(request.get_json(silent=True) or {})
    except ValidationError as error:
        return jsonify({"error": _validation_error_message(error)}), 400

    recipe = Recipe(
        user_id=current_user.id,
        title=_EXTRACTION_PLACEHOLDER_TITLE,
        image_s3_key=payload.s3_key,
        origin=payload.origin,
        source_url=payload.source_url,
        extraction_status="pending",
        cook_count=0,
    )
    db.session.add(recipe)
    db.session.commit()

    try:
        sqs.enqueue_extraction(recipe.id, payload.s3_key, current_user.id)
    except Exception as error:  # noqa: BLE001 - any enqueue failure is handled the same
        # Roll back the recipe so we never leave an orphaned ``pending`` row
        # whose extraction job never reached the queue.
        logger.warning(
            "Failed to enqueue extraction for recipe %s (rolling back): %s",
            recipe.id,
            error,
        )
        db.session.delete(recipe)
        db.session.commit()
        return (
            jsonify({"error": "Failed to enqueue extraction; please retry"}),
            503,
        )

    response = ExtractResponse(
        recipe_id=recipe.id,
        extraction_status=recipe.extraction_status,
    )
    return jsonify(response.model_dump(mode="json")), 201


@recipes_bp.get("/<uuid:recipe_id>/extraction-status")
@require_auth
def get_extraction_status(recipe_id):
    """Report the current extraction status of a recipe owned by the caller.

    Implements ``GET /recipes/{id}/extraction-status`` from ``docs/api.md``:
    the poll endpoint a client hits after ``POST /recipes/extract`` to watch
    the job progress through ``pending`` → ``processing`` → ``complete`` /
    ``failed``.

    Lookup and ownership reuse :func:`_get_owned_recipe_or_error`, matching the
    other ``/{id}`` endpoints exactly: a missing or soft-deleted recipe returns
    404, a recipe owned by another user returns 403.

    Args:
        recipe_id: The recipe UUID parsed from the path by Flask's ``uuid``
            converter. The typed converter and the distinct ``/extraction-status``
            suffix keep this route from colliding with ``GET /recipes/{id}``.

    Returns:
        JSON ``{"extraction_status": "pending|processing|complete|failed"}``
        with HTTP 200.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    response = ExtractionStatusResponse(extraction_status=recipe.extraction_status)
    return jsonify(response.model_dump(mode="json")), 200


@recipes_bp.get("/<uuid:recipe_id>")
@require_auth
def get_recipe(recipe_id):
    """Return the full detail for a single recipe owned by the caller.

    Looks the recipe up by id and returns the complete
    :class:`~app.schemas.recipe.RecipeDetail` object — scalars plus the ordered
    nested ingredients/tools/instructions, tag names, and the
    ``cook_summary`` — as documented in ``docs/api.md`` under
    ``GET /recipes/{id}``. The ``<uuid:...>`` converter means a non-UUID path
    segment never reaches this view and Flask returns a 404 on its own.

    A missing or soft-deleted recipe is reported as 404. A recipe owned by a
    different user is rejected with 403.

    Ownership-vs-privacy note: returning 403 for a recipe owned by someone else
    confirms to a non-owner that the recipe exists, a minor existence leak that
    a 404-everywhere policy would avoid. ``docs/api.md`` explicitly documents
    403 as "authenticated but not the resource owner", so we follow the
    documented contract and return 403 here.

    Args:
        recipe_id: The recipe UUID parsed from the path by Flask's ``uuid``
            converter.

    Returns:
        JSON of the full :class:`RecipeDetail` object with HTTP 200.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    detail = RecipeDetail.from_model(recipe)
    return jsonify(detail.model_dump(mode="json")), 200


@recipes_bp.patch("/<uuid:recipe_id>")
@require_auth
def update_recipe(recipe_id):
    """Partially update a single recipe owned by the caller.

    Implements ``PATCH /recipes/{id}`` from ``docs/api.md``: the body carries
    the same fields as ``POST /recipes`` but every field is optional. Only the
    fields the client actually sends are applied — ``model_dump(
    exclude_unset=True)`` distinguishes an omitted field (left untouched) from
    one explicitly set to ``null`` (cleared). Every nullable column may be
    cleared this way; ``title`` is the exception and cannot be set to
    ``null``/empty (:class:`~app.schemas.recipe.RecipeUpdate` rejects it).

    Lookup and ownership follow the exact same rules as ``GET /recipes/{id}``
    via :func:`_get_owned_recipe_or_error`: 404 for a missing/soft-deleted
    recipe, 403 for one owned by another user. A validation failure (empty
    ``title``, bad ``origin``, negative times/servings, unknown field) returns
    400 with a concise ``{"error": ...}`` body via the shared helper.

    ``updated_at`` advances automatically: the column declares
    ``onupdate=datetime.utcnow``, which SQLAlchemy applies on every flush of a
    dirty row (and which works on SQLite as well as Postgres), so no explicit
    assignment is needed.

    Args:
        recipe_id: The recipe UUID parsed from the path by Flask's ``uuid``
            converter.

    Returns:
        JSON of the updated full :class:`RecipeDetail` object with HTTP 200.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    try:
        payload = RecipeUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as validation_error:
        return jsonify({"error": _validation_error_message(validation_error)}), 400

    # Apply only the fields the client actually sent. ``exclude_unset=True``
    # keeps omitted fields untouched while still allowing an explicit ``null``
    # to clear a nullable column.
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(recipe, field, value)

    # ``updated_at`` is bumped by the model's ``onupdate`` on commit.
    db.session.commit()

    detail = RecipeDetail.from_model(recipe)
    return jsonify(detail.model_dump(mode="json")), 200


@recipes_bp.delete("/<uuid:recipe_id>")
@require_auth
def delete_recipe(recipe_id):
    """Soft-delete a single recipe owned by the caller.

    Implements ``DELETE /recipes/{id}`` from ``docs/api.md``: the recipe is
    not physically removed; instead its ``deleted_at`` timestamp is stamped
    with the current UTC time. The existing read endpoints
    (:func:`list_recipes` and :func:`get_recipe` via
    :func:`_get_owned_recipe_or_error`) already filter on
    ``deleted_at IS NULL``, so a soft-deleted recipe immediately disappears
    from ``GET /recipes`` and returns 404 from ``GET /recipes/{id}``.

    Lookup and ownership reuse :func:`_get_owned_recipe_or_error`, matching
    ``GET``/``PATCH`` exactly: a missing recipe returns 404, a recipe owned by
    another user returns 403, and an *already soft-deleted* recipe is treated
    as missing and returns 404. The latter makes a second DELETE a no-op 404
    rather than a 204 — close to idempotent in effect (the resource stays
    deleted) while still signalling that there was nothing left to delete.

    Args:
        recipe_id: The recipe UUID parsed from the path by Flask's ``uuid``
            converter. A non-UUID segment never routes here (Flask returns 404
            on its own).

    Returns:
        An empty body with HTTP 204 on success.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    recipe.deleted_at = datetime.datetime.utcnow()
    db.session.commit()

    return "", 204


def _find_or_create_tag(name: str) -> Tag:
    """Return the global :class:`Tag` with ``name``, creating it if absent.

    ``tags.name`` is **globally unique** — tags are a shared pool keyed by
    name, so the same row is reused across every user and recipe that applies
    that label. "Create the tag if it doesn't exist" therefore means: look the
    tag up by name and insert one only when none exists yet.

    Concurrency mirrors the user-provisioning path in
    :func:`app.auth.decorators._provision_user`: two requests can both see "no
    such tag" and race to insert. The unique constraint settles the tie — the
    loser catches :class:`~sqlalchemy.exc.IntegrityError`, rolls back, and
    re-queries to return whichever row the winner committed.

    Args:
        name: The already-validated/stripped tag name.

    Returns:
        The existing or newly created :class:`Tag`.
    """
    existing = Tag.query.filter_by(name=name).first()
    if existing is not None:
        return existing

    tag = Tag(name=name)
    db.session.add(tag)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent request inserted the same name first. Roll back our
        # failed insert and return theirs.
        db.session.rollback()
        winner = Tag.query.filter_by(name=name).first()
        if winner is None:
            # The conflict was not on ``name`` (unexpected) — surface it.
            raise
        return winner

    return tag


@recipes_bp.post("/<uuid:recipe_id>/tags")
@require_auth
def add_tag(recipe_id):
    """Add a tag to a recipe owned by the caller, creating the tag if needed.

    Implements ``POST /recipes/{id}/tags`` from ``docs/api.md``. The body is
    ``{"name": "<tag>"}``. The view:

    1. resolves+owns the recipe via :func:`_get_owned_recipe_or_error` (404 for
       a missing/soft-deleted recipe, 403 for one owned by another user) —
       identical to the rest of the ``/{id}`` surface,
    2. validates the body (400 on a missing/empty ``name``, over-long name, or
       unknown field),
    3. find-or-creates the **global** tag by name
       (:func:`_find_or_create_tag`) — because ``tags.name`` is globally
       unique, an existing tag (even one only used by another user) is reused,
       so the returned ``id`` is stable across users,
    4. associates it with the recipe, idempotently.

    Idempotency and status code
    ----------------------------
    If the recipe already carries the tag, no duplicate ``recipe_tags`` row is
    created and the request still succeeds, returning the tag with **200**. A
    newly created association returns **201**. This lets a client distinguish
    "I just added it" from "it was already there" while keeping repeat calls
    safe.

    Returns:
        JSON of the :class:`TagResponse` (``{"id", "name"}``) with HTTP 201 when
        the association is newly created, or 200 when it already existed.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    try:
        payload = TagAddRequest.model_validate(request.get_json(silent=True) or {})
    except ValidationError as validation_error:
        return jsonify({"error": _validation_error_message(validation_error)}), 400

    tag = _find_or_create_tag(payload.name)

    # Is this tag already attached to the recipe? Treat a repeat add as an
    # idempotent success rather than creating a duplicate join row.
    already_present = (
        RecipeTag.query.filter_by(recipe_id=recipe.id, tag_id=tag.id).first()
        is not None
    )

    if already_present:
        response = TagResponse.model_validate(tag)
        return jsonify(response.model_dump(mode="json")), 200

    db.session.add(RecipeTag(recipe_id=recipe.id, tag_id=tag.id))
    db.session.commit()

    response = TagResponse.model_validate(tag)
    return jsonify(response.model_dump(mode="json")), 201


@recipes_bp.delete("/<uuid:recipe_id>/tags/<uuid:tag_id>")
@require_auth
def remove_tag(recipe_id, tag_id):
    """Remove a tag association from a recipe owned by the caller.

    Implements ``DELETE /recipes/{id}/tags/{tag_id}`` from ``docs/api.md``.
    Lookup and ownership reuse :func:`_get_owned_recipe_or_error` (404 for a
    missing/soft-deleted recipe, 403 for one owned by another user), matching
    the rest of the ``/{id}`` surface.

    This deletes only the **association** (the ``recipe_tags`` row); the global
    :class:`Tag` row is intentionally left intact because tags are a shared
    pool — other users' recipes (or the caller's other recipes) may still use
    it, and deleting the global row would strip the tag from them too.

    If the recipe does not carry the given tag, the request returns **404**
    (``"Tag not found on recipe"``) — there is nothing to remove. This covers
    both an unknown ``tag_id`` and a real tag that is simply not attached here.

    Returns:
        An empty body with HTTP 204 when the association was removed.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    association = RecipeTag.query.filter_by(
        recipe_id=recipe.id, tag_id=tag_id
    ).first()
    if association is None:
        return jsonify({"error": "Tag not found on recipe"}), 404

    db.session.delete(association)
    db.session.commit()

    return "", 204
