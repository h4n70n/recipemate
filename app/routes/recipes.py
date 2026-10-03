"""Recipe resource endpoints.

Houses the recipe CRUD surface documented in ``docs/api.md`` under
``/v1/recipes``. Currently implements ``GET /recipes`` (paginated list of the
authenticated user's recipes); the remaining CRUD endpoints are added by their
respective tasks.
"""

from __future__ import annotations

import datetime

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app import db
from app.auth import AuthError, current_user, require_auth
from app.models.recipe import Recipe
from app.schemas.recipe import (
    RecipeCard,
    RecipeCreate,
    RecipeDetail,
    RecipeListQuery,
    RecipeListResponse,
    RecipeUpdate,
)
from app.services.urls import s3_key_to_url

recipes_bp = Blueprint("recipes", __name__, url_prefix="/v1/recipes")


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
