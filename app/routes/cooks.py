"""Cook log endpoints (TASK-4.1).

Implements the recipe-scoped cook log CRUD surface documented in
``docs/api.md`` under *Cook Logs*. Every path is nested under its parent
recipe at ``/v1/recipes/{id}/cooks[...]``:

* ``GET    /recipes/{id}/cooks``            — list the recipe's cook logs,
  reverse-chronological.
* ``POST   /recipes/{id}/cooks``            — add a cook log.
* ``PATCH  /recipes/{id}/cooks/{cook_id}``  — update notes, rating, or photo.
* ``DELETE /recipes/{id}/cooks/{cook_id}``  — delete a cook log.

Blueprint choice
----------------
These routes live on ``cooks_bp`` (``url_prefix="/v1/recipes"``) rather than on
``recipes_bp`` so all cook-log code stays in this module. Because Flask merges
every blueprint into one URL map, the paths must simply not collide with
``recipes_bp``'s routes — and they don't: ``/<uuid:recipe_id>/cooks`` and
``/<uuid:recipe_id>/cooks/<uuid:cook_id>`` have no equivalent on ``recipes_bp``
(whose nested routes are ``/<id>/tags[...]`` and ``/<id>/extraction-status``).

Shared ownership rules
----------------------
The parent recipe is resolved for every endpoint via
:func:`app.routes.recipes._get_owned_recipe_or_error`, so cook logs enforce the
exact same lookup/ownership contract as the recipe endpoints: 404 for a
missing/soft-deleted recipe, 403 for a recipe owned by another user. That
helper is imported here (not duplicated); there is no import cycle because
``recipes`` does not import ``cooks`` — both are imported independently by the
app factory.

Denormalised stats
------------------
``Recipe.cook_count`` and ``Recipe.avg_rating`` are recomputed from the
recipe's cook logs by :func:`recalculate_recipe_cook_stats` after every create,
update, and delete, within the same transaction as the write.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from app import db
from app.auth import current_user, require_auth
from app.models.cook_log import CookLog
from app.routes.recipes import (
    _get_owned_recipe_or_error,
    _validation_error_message,
)
from app.schemas.cook_log import (
    CookLogCreate,
    CookLogListResponse,
    CookLogResponse,
    CookLogUpdate,
)
from app.services.urls import s3_key_to_url

cooks_bp = Blueprint("cooks", __name__, url_prefix="/v1/recipes")


def recalculate_recipe_cook_stats(recipe) -> None:
    """Recompute ``cook_count`` and ``avg_rating`` on ``recipe`` from its logs.

    Called after every cook log create, update, or delete so the recipe's two
    denormalised columns stay in sync with the underlying ``cook_logs`` rows.
    It only mutates the in-memory ORM instance; the caller is responsible for
    committing, so the recalculation lands in the **same transaction** as the
    write that triggered it.

    Rules (from the task + ``docs/api.md``):

    * ``cook_count`` is the total number of cook logs for the recipe —
      **every** log counts, rated or not.
    * ``avg_rating`` is the mean of the **non-null** ratings only, rounded to
      two decimals (the column is ``Numeric(3, 2)``). Logs without a rating are
      **excluded** from the average — they are not treated as a 0. When no log
      has a rating, ``avg_rating`` is ``None`` (NULL), not 0.

    Args:
        recipe: The parent :class:`~app.models.recipe.Recipe` ORM instance.
    """
    logs = recipe.cook_logs.all()

    recipe.cook_count = len(logs)

    ratings = [log.rating for log in logs if log.rating is not None]
    if ratings:
        average = Decimal(sum(ratings)) / Decimal(len(ratings))
        recipe.avg_rating = average.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    else:
        recipe.avg_rating = None


def _cook_log_response(cook_log: CookLog):
    """Serialize a cook log, deriving ``photo_url`` from its stored key."""
    response = CookLogResponse.from_model(
        cook_log, photo_url=s3_key_to_url(cook_log.photo_s3_key)
    )
    return response.model_dump(mode="json")


def _get_cook_log_or_404(recipe, cook_id):
    """Fetch a cook log by id **scoped to** ``recipe``, or report a 404.

    Scoping the lookup to the recipe is what makes cross-recipe access fail:
    a ``cook_id`` that is a valid UUID but belongs to a different recipe is not
    found on *this* recipe and returns 404 (not another recipe's log). A
    non-UUID ``cook_id`` never reaches the view — Flask's ``uuid`` converter
    rejects it with its own 404.

    Args:
        recipe: The already-resolved, owned parent recipe.
        cook_id: The cook log UUID parsed from the path.

    Returns:
        A ``(cook_log, error_response)`` pair: on success ``cook_log`` is the
        ORM record and ``error_response`` is ``None``; when absent on this
        recipe ``cook_log`` is ``None`` and ``error_response`` is the
        ``(json, 404)`` tuple the view returns as-is.
    """
    cook_log = CookLog.query.filter_by(id=cook_id, recipe_id=recipe.id).first()
    if cook_log is None:
        return None, (jsonify({"error": "Cook log not found"}), 404)
    return cook_log, None


@cooks_bp.get("/<uuid:recipe_id>/cooks")
@require_auth
def list_cook_logs(recipe_id):
    """List a recipe's cook logs, reverse-chronological.

    Implements ``GET /recipes/{id}/cooks`` from ``docs/api.md``. Ownership and
    lookup of the parent recipe reuse
    :func:`~app.routes.recipes._get_owned_recipe_or_error` (404 missing/
    soft-deleted, 403 non-owner).

    Ordering is newest cook first: by ``cooked_at`` descending, with
    ``created_at`` descending as the tiebreak so two logs with the same cook
    date fall back to insertion order (most recently recorded first).

    Returns:
        JSON ``{"cooks": [<cook log>, ...]}`` with HTTP 200.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    logs = (
        recipe.cook_logs.order_by(
            CookLog.cooked_at.desc(), CookLog.created_at.desc()
        ).all()
    )

    response = CookLogListResponse(
        cooks=[
            CookLogResponse.from_model(
                log, photo_url=s3_key_to_url(log.photo_s3_key)
            )
            for log in logs
        ]
    )
    return jsonify(response.model_dump(mode="json")), 200


@cooks_bp.post("/<uuid:recipe_id>/cooks")
@require_auth
def create_cook_log(recipe_id):
    """Add a cook log to a recipe owned by the caller.

    Implements ``POST /recipes/{id}/cooks`` from ``docs/api.md``. The parent
    recipe is resolved/owned via
    :func:`~app.routes.recipes._get_owned_recipe_or_error`. The body is
    validated by :class:`~app.schemas.cook_log.CookLogCreate` — ``cooked_at``
    required, ``notes``/``rating``/``photo_s3_key`` optional, ``rating`` 1..5,
    unknown fields rejected — with failures returned as a concise 400 via the
    shared helper.

    The new log is owned by the authenticated caller (``user_id``) and attached
    to the recipe. After inserting it the parent recipe's ``cook_count`` and
    ``avg_rating`` are recalculated (:func:`recalculate_recipe_cook_stats`) in
    the same transaction before committing.

    ``photo_s3_key`` is wired straight through from the shared
    ``POST /recipes/upload`` presigned flow; the response's ``photo_url`` is
    derived from it.

    Returns:
        JSON of the created :class:`~app.schemas.cook_log.CookLogResponse` with
        HTTP 201.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    try:
        payload = CookLogCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as validation_error:
        return jsonify({"error": _validation_error_message(validation_error)}), 400

    cook_log = CookLog(
        recipe_id=recipe.id,
        user_id=current_user.id,
        cooked_at=payload.cooked_at,
        notes=payload.notes,
        rating=payload.rating,
        photo_s3_key=payload.photo_s3_key,
    )
    db.session.add(cook_log)

    # Recompute the parent's denormalised stats in the same transaction. The
    # new log is pending in the session; flushing makes it visible to the
    # relationship query inside the recalculation.
    db.session.flush()
    recalculate_recipe_cook_stats(recipe)
    db.session.commit()

    return jsonify(_cook_log_response(cook_log)), 201


@cooks_bp.patch("/<uuid:recipe_id>/cooks/<uuid:cook_id>")
@require_auth
def update_cook_log(recipe_id, cook_id):
    """Update a cook log's notes, rating, or photo.

    Implements ``PATCH /recipes/{id}/cooks/{cook_id}`` from ``docs/api.md``.
    The parent recipe is resolved/owned via
    :func:`~app.routes.recipes._get_owned_recipe_or_error`; the cook log is
    then looked up **scoped to that recipe** (:func:`_get_cook_log_or_404`), so
    a log that belongs to a different recipe returns 404.

    Only the fields the client actually sent are applied
    (``model_dump(exclude_unset=True)``), which lets an explicit ``null`` clear
    ``notes``/``rating``/``photo_s3_key`` while an omitted field is left
    untouched. ``cooked_at`` is not an accepted field — sending it is a 400
    (unknown field) via :class:`~app.schemas.cook_log.CookLogUpdate`.

    Because the ``rating`` may have changed (or been cleared), the parent
    recipe's ``avg_rating``/``cook_count`` are recalculated in the same
    transaction before committing.

    Returns:
        JSON of the updated :class:`~app.schemas.cook_log.CookLogResponse` with
        HTTP 200.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    cook_log, not_found = _get_cook_log_or_404(recipe, cook_id)
    if not_found is not None:
        return not_found

    try:
        payload = CookLogUpdate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as validation_error:
        return jsonify({"error": _validation_error_message(validation_error)}), 400

    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(cook_log, field, value)

    # The rating may have changed/cleared — recompute the parent's stats.
    db.session.flush()
    recalculate_recipe_cook_stats(recipe)
    db.session.commit()

    return jsonify(_cook_log_response(cook_log)), 200


@cooks_bp.delete("/<uuid:recipe_id>/cooks/<uuid:cook_id>")
@require_auth
def delete_cook_log(recipe_id, cook_id):
    """Delete a cook log and recompute the parent recipe's stats.

    Implements ``DELETE /recipes/{id}/cooks/{cook_id}`` from ``docs/api.md``.
    The parent recipe is resolved/owned via
    :func:`~app.routes.recipes._get_owned_recipe_or_error`; the cook log is
    looked up **scoped to that recipe** (:func:`_get_cook_log_or_404`), so a
    log on a different recipe (or an unknown id) returns 404.

    After deleting the log the parent recipe's ``cook_count`` and
    ``avg_rating`` are recalculated in the same transaction before committing —
    so deleting the last rated log leaves ``avg_rating`` NULL.

    Returns:
        An empty body with HTTP 204 on success.
    """
    recipe, error = _get_owned_recipe_or_error(recipe_id)
    if error is not None:
        return error

    cook_log, not_found = _get_cook_log_or_404(recipe, cook_id)
    if not_found is not None:
        return not_found

    db.session.delete(cook_log)

    db.session.flush()
    recalculate_recipe_cook_stats(recipe)
    db.session.commit()

    return "", 204
