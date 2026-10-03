"""Request-level authentication for the RecipeMate Flask API.

This module layers a :func:`require_auth` view decorator on top of the token
verification primitives in :mod:`app.auth.jwt`. The decorator pulls the bearer
token off the incoming request, validates it, resolves the matching
:class:`~app.models.user.User`, and stashes that user on Flask's request-scoped
:data:`flask.g` so view functions (and the :data:`current_user` proxy) can read
it without threading it through arguments.

User resolution lives in :func:`resolve_current_user`: it looks up an existing
user by ``cognito_sub`` and, on the first authenticated request for a new
subject, lazily provisions one via :func:`_provision_user`.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from typing import Any, Callable, TypeVar, cast, overload

from flask import g, request
from sqlalchemy.exc import IntegrityError
from werkzeug.local import LocalProxy

from app.auth.jwt import AuthError, validate_token

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# current_user proxy
# ---------------------------------------------------------------------------


def _get_current_user() -> Any:
    """Return the user resolved for the active request.

    Raises:
        RuntimeError: If accessed outside a request that passed through
            :func:`require_auth` (i.e. ``g.current_user`` was never set).
    """
    user = getattr(g, "current_user", None)
    if user is None:
        raise RuntimeError(
            "current_user is only available inside a @require_auth view"
        )
    return user


#: Proxy bound to ``g.current_user``. Import this to reference the authenticated
#: user from view code: ``from app.auth import current_user``.
current_user = LocalProxy(_get_current_user)


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


def _extract_bearer_token() -> str:
    """Pull the bearer token out of the ``Authorization`` request header.

    Returns:
        The raw JWT string (without the ``Bearer `` prefix).

    Raises:
        AuthError: If the header is absent or not a well-formed
            ``Bearer <token>`` value.
    """
    header = request.headers.get("Authorization")
    if not header:
        raise AuthError("Authorization header missing", 401)

    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise AuthError("Authorization header must be 'Bearer <token>'", 401)

    return parts[1]


# ---------------------------------------------------------------------------
# User resolution
# ---------------------------------------------------------------------------


def _provision_user(claims: dict[str, Any]) -> Any:
    """Create (lazily provision) a user from verified token claims.

    Called by :func:`resolve_current_user` the first time an authenticated
    request arrives for a Cognito subject we have never seen. It inserts a
    ``users`` row keyed by ``cognito_sub`` and returns the freshly created
    :class:`~app.models.user.User`.

    Concurrency: two in-flight first requests for the same subject can both
    pass the "no existing user" check and race to insert. The unique
    constraint on ``cognito_sub`` lets the database settle the tie — the loser
    catches :class:`~sqlalchemy.exc.IntegrityError`, rolls back, and re-queries
    to return whichever row the winner committed.

    Args:
        claims: The verified JWT claims. Always contains ``sub``; ID tokens
            also carry ``email``, but access tokens may not.

    Returns:
        The newly created (or concurrently created) :class:`User`.
    """
    from app import db  # noqa: PLC0415
    from app.models.user import User  # noqa: PLC0415

    cognito_sub = claims["sub"]

    # ID tokens include ``email``; access tokens may not. Fall back to the
    # Cognito username, then to a deterministic placeholder derived from the
    # subject. ``email`` is NOT NULL + unique, so we must supply something
    # distinct. The placeholder can be backfilled later once an ID token (or a
    # profile call) gives us the real address.
    email = claims.get("email") or claims.get("username")
    if not email:
        email = f"{cognito_sub}@users.noreply.recipemate"

    user = User(cognito_sub=cognito_sub, email=email)
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent first request won the race and already inserted this
        # subject (or email). Roll our failed insert back and return theirs.
        db.session.rollback()
        existing = User.query.filter_by(cognito_sub=cognito_sub).first()
        if existing is None:
            # The conflict was not on cognito_sub (unexpected) — surface it.
            raise
        return existing

    return user


def resolve_current_user(claims: dict[str, Any]) -> Any:
    """Resolve the authenticated :class:`User` from verified token claims.

    Looks the user up by ``cognito_sub`` (the token's ``sub`` claim). If no row
    exists yet, it is lazily provisioned via :func:`_provision_user`.

    Args:
        claims: The verified JWT claims.

    Returns:
        The resolved :class:`~app.models.user.User` instance.

    Raises:
        AuthError: If the ``sub`` claim is missing.
    """
    # Imported lazily to avoid a circular import at module load time
    # (models import ``app.db``, which imports the app package).
    from app.models.user import User  # noqa: PLC0415

    cognito_sub = claims.get("sub")
    if not cognito_sub:
        raise AuthError("Token is missing a subject claim", 401)

    user = User.query.filter_by(cognito_sub=cognito_sub).first()
    if user is not None:
        return user

    # No existing user — lazily provision one from the token claims.
    return _provision_user(claims)


# ---------------------------------------------------------------------------
# Scope handling
# ---------------------------------------------------------------------------


def _extract_token_scopes(claims: dict[str, Any]) -> set[str]:
    """Return the set of OAuth scopes granted by a verified token.

    Cognito access tokens put granted scopes in a space-delimited ``scope``
    string (e.g. ``"recipes:read recipes:write"``). Some deployments model
    coarse permissions as Cognito groups, which appear in ``cognito:groups``
    as a list. Both sources are merged so either style can satisfy a scope
    requirement.

    Args:
        claims: The verified JWT claims.

    Returns:
        The set of scope strings present on the token (empty if none).
    """
    scopes: set[str] = set()

    scope_claim = claims.get("scope")
    if isinstance(scope_claim, str):
        scopes.update(scope_claim.split())

    groups_claim = claims.get("cognito:groups")
    if isinstance(groups_claim, str):
        scopes.update(groups_claim.split())
    elif isinstance(groups_claim, Iterable):
        scopes.update(str(group) for group in groups_claim)

    return scopes


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def _build_wrapper(view: F, required_scopes: frozenset[str]) -> F:
    """Wrap ``view`` with token validation, user resolution, and scope checks.

    Args:
        view: The Flask view function to protect.
        required_scopes: Scopes the token must carry. Empty means any
            authenticated caller is allowed.

    Returns:
        The wrapped view function.
    """

    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _extract_bearer_token()
        claims = validate_token(token)

        if required_scopes:
            granted = _extract_token_scopes(claims)
            if not required_scopes.issubset(granted):
                raise AuthError("Insufficient scope", 403)

        g.current_user = resolve_current_user(claims)
        return view(*args, **kwargs)

    return cast(F, wrapper)


@overload
def require_auth(view: F) -> F: ...


@overload
def require_auth(
    *, scopes: Iterable[str] | None = ...
) -> Callable[[F], F]: ...


def require_auth(
    view: F | None = None, *, scopes: Iterable[str] | None = None
) -> F | Callable[[F], F]:
    """Protect a Flask view: validate the bearer token and inject the user.

    On a successful request this extracts and validates the ``Authorization:
    Bearer <token>`` header, resolves the matching user, binds it to
    ``g.current_user`` (also reachable via the :data:`current_user` proxy), and
    then invokes the wrapped view, returning its response unchanged.

    Supports two call forms:

    * Bare — ``@require_auth`` — any authenticated caller is allowed.
    * Parameterised — ``@require_auth(scopes=["recipes:write"])`` — the token
      must additionally carry every listed scope, or the request is rejected
      with ``AuthError("Insufficient scope", 403)``.

    Any :class:`AuthError` raised during extraction, validation, scope
    enforcement, or resolution propagates out of the view and is turned into a
    JSON error response by the error handler registered in
    :func:`app.create_app`. Missing or invalid tokens surface as ``401`` while
    an authenticated token lacking a required scope surfaces as ``403``.

    Args:
        view: The Flask view function to wrap. Present only when used as a bare
            decorator; ``None`` when invoked with keyword arguments.
        scopes: Optional OAuth scopes the token must contain. Defaults to no
            scope requirement.

    Returns:
        Either the wrapped view (bare usage) or a decorator that wraps a view
        (parameterised usage).
    """
    required_scopes = frozenset(scopes or ())

    # Bare usage: @require_auth  ->  require_auth(view)
    if view is not None:
        return _build_wrapper(view, required_scopes)

    # Parameterised usage: @require_auth(scopes=[...])  ->  decorator factory
    def decorator(inner_view: F) -> F:
        return _build_wrapper(inner_view, required_scopes)

    return decorator
