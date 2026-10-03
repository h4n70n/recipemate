"""Tag resource endpoints.

Houses the top-level ``/v1/tags`` surface — currently ``GET /tags``, which
powers tag autocomplete by listing the tags the authenticated user actually
uses. The recipe-scoped tag endpoints (``POST``/``DELETE`` under
``/v1/recipes/{id}/tags``) live on ``recipes_bp`` in :mod:`app.routes.recipes`,
since they are keyed by a recipe; this module only owns the user-scoped listing
whose path is not nested under a recipe.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from app.auth import current_user, require_auth
from app.models.recipe import Recipe, RecipeTag, Tag
from app.schemas.recipe import TagListResponse, TagResponse

tags_bp = Blueprint("tags", __name__, url_prefix="/v1/tags")


@tags_bp.get("")
@require_auth
def list_tags():
    """List the tags the authenticated user actually uses, for autocomplete.

    Implements ``GET /tags`` from ``docs/api.md``. Because ``tags.name`` is
    globally unique — tags are a shared pool across all users — "tags used by
    the current user" is **not** every tag in the table. It is the set of
    distinct tags attached to the caller's own recipes, so the result is
    derived by joining ``tags → recipe_tags → recipes`` and filtering to the
    caller:

    * ``recipes.user_id == current_user.id`` — only the caller's recipes,
    * ``recipes.deleted_at IS NULL`` — a tag whose only association is a
      soft-deleted recipe does not appear (that recipe is logically gone).

    Results are ``DISTINCT`` (a tag used on several of the caller's recipes
    appears once) and ordered by name for a stable, predictable autocomplete
    list.

    Returns:
        JSON ``{"tags": [{"id", "name"}, ...]}`` with HTTP 200.
    """
    tags = (
        Tag.query.join(RecipeTag, RecipeTag.tag_id == Tag.id)
        .join(Recipe, Recipe.id == RecipeTag.recipe_id)
        .filter(
            Recipe.user_id == current_user.id,
            Recipe.deleted_at.is_(None),
        )
        .distinct()
        .order_by(Tag.name)
        .all()
    )

    response = TagListResponse(
        tags=[TagResponse.model_validate(tag) for tag in tags]
    )
    return jsonify(response.model_dump(mode="json")), 200
