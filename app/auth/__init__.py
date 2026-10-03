"""Authentication package for RecipeMate.

Exposes Cognito JWT validation primitives together with the request-level
:func:`require_auth` decorator and the :data:`current_user` proxy.
"""

from app.auth.decorators import current_user, require_auth, resolve_current_user
from app.auth.jwt import AuthError, validate_token

__all__ = [
    "AuthError",
    "current_user",
    "require_auth",
    "resolve_current_user",
    "validate_token",
]
