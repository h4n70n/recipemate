"""Cognito JWT validation for the RecipeMate Flask API.

This module verifies ``RS256``-signed JSON Web Tokens issued by an Amazon
Cognito User Pool. It fetches the pool's public signing keys (the JWKS) from
Cognito's well-known endpoint and caches them in-process so they are not
re-fetched on every request.

The public surface is intentionally small:

* :class:`AuthError` -- raised for any authentication failure, carrying a
  human-readable message and an HTTP status code.
* :func:`validate_token` -- verifies a raw bearer token and returns its claims.

The ``@require_auth`` decorator is built on top of :func:`validate_token` in a
separate module; this one only concerns itself with turning a token string into
a trusted claims dict (or an error).
"""

from __future__ import annotations

from typing import Any

import requests
from flask import current_app
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError, JWTError

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Raised when a token cannot be validated.

    Args:
        message: A human-readable explanation safe to surface to clients.
        status_code: The HTTP status code the API should respond with.
            Defaults to ``401`` (Unauthorized).
    """

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuthError status_code={self.status_code} message={self.message!r}>"


# ---------------------------------------------------------------------------
# JWKS fetching & caching
# ---------------------------------------------------------------------------

# Module-level cache keyed by the JWKS URL. Each entry maps ``kid`` -> JWK dict.
# Tests may populate or clear this directly to avoid network access.
_JWKS_CACHE: dict[str, dict[str, dict[str, Any]]] = {}

# How long to wait on the network call to Cognito before giving up.
_JWKS_FETCH_TIMEOUT_SECONDS = 5


def _config_value(key: str) -> str:
    """Read a required Cognito config value from the active Flask app.

    Raises:
        AuthError: If no application context is available or the value is unset.
    """
    try:
        value = current_app.config.get(key, "")
    except RuntimeError as exc:  # outside application context
        raise AuthError("Authentication is not configured", status_code=500) from exc
    if not value:
        raise AuthError(
            f"Missing Cognito configuration: {key}", status_code=500
        )
    return value


def _issuer() -> str:
    """Return the expected ``iss`` claim for the configured user pool."""
    region = _config_value("COGNITO_REGION")
    user_pool_id = _config_value("COGNITO_USER_POOL_ID")
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def _jwks_url() -> str:
    """Return the well-known JWKS URL for the configured user pool."""
    return f"{_issuer()}/.well-known/jwks.json"


def get_jwks(*, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Return the user pool's JWKS as a ``kid`` -> key mapping, cached in-process.

    The keys are fetched from Cognito's well-known endpoint on first use and
    reused thereafter. Pass ``force_refresh=True`` to bypass the cache (useful
    when a token references a ``kid`` not present in the cached set, e.g. after
    Cognito rotates its signing keys).

    Args:
        force_refresh: When ``True``, always re-fetch from the network.

    Returns:
        A mapping of key id (``kid``) to the raw JWK dict.

    Raises:
        AuthError: If the JWKS cannot be fetched or parsed.
    """
    url = _jwks_url()
    if not force_refresh and url in _JWKS_CACHE:
        return _JWKS_CACHE[url]

    try:
        response = requests.get(url, timeout=_JWKS_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AuthError(
            "Unable to fetch signing keys", status_code=503
        ) from exc

    keys = {key["kid"]: key for key in payload.get("keys", []) if "kid" in key}
    if not keys:
        raise AuthError("No signing keys available", status_code=503)

    _JWKS_CACHE[url] = keys
    return keys


def _find_signing_key(kid: str) -> dict[str, Any]:
    """Return the JWK matching ``kid``, refreshing the cache once if needed."""
    keys = get_jwks()
    if kid in keys:
        return keys[kid]

    # The kid may be new (key rotation). Refresh once before giving up.
    keys = get_jwks(force_refresh=True)
    if kid in keys:
        return keys[kid]

    raise AuthError("Signing key not found for token")


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


def validate_token(token: str) -> dict[str, Any]:
    """Validate a Cognito-issued JWT and return its decoded claims.

    Verifies, in order: the token structure and ``kid`` header, the RS256
    signature against the matching JWKS public key, expiry, issuer, and
    audience/client id.

    Cognito ID tokens carry the client id in the ``aud`` claim, while access
    tokens carry it in ``client_id`` and omit ``aud``. Both are accepted as
    long as the client id matches ``COGNITO_CLIENT_ID``.

    Args:
        token: The raw JWT string (without the ``Bearer `` prefix).

    Returns:
        The decoded claims dictionary.

    Raises:
        AuthError: If the token is missing, malformed, expired, or fails any
            signature or claim check.
    """
    if not token:
        raise AuthError("Authorization token is missing")

    client_id = _config_value("COGNITO_CLIENT_ID")
    issuer = _issuer()

    # 1. Read the (unverified) header to find which key signed this token.
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise AuthError("Malformed authentication token") from exc

    kid = header.get("kid")
    if not kid:
        raise AuthError("Token header is missing a key id")

    # 2. Resolve the matching public key from the (cached) JWKS.
    signing_key = _find_signing_key(kid)

    # 3. Determine where the client id lives. Access tokens have no ``aud``,
    #    so we only ask python-jose to verify ``aud`` for ID tokens and check
    #    ``client_id`` ourselves for access tokens.
    unverified_claims = jwt.get_unverified_claims(token)
    is_access_token = "aud" not in unverified_claims

    decode_kwargs: dict[str, Any] = {
        "algorithms": ["RS256"],
        "issuer": issuer,
    }
    if is_access_token:
        decode_kwargs["options"] = {"verify_aud": False}
    else:
        decode_kwargs["audience"] = client_id

    # 4. Verify signature, expiry, issuer, and (for ID tokens) audience.
    try:
        claims = jwt.decode(token, signing_key, **decode_kwargs)
    except ExpiredSignatureError as exc:
        raise AuthError("Authentication token has expired") from exc
    except JWTClaimsError as exc:
        raise AuthError("Authentication token has invalid claims") from exc
    except JWTError as exc:
        raise AuthError("Authentication token signature is invalid") from exc

    # 5. For access tokens, enforce the client id ourselves.
    if is_access_token and claims.get("client_id") != client_id:
        raise AuthError("Authentication token audience is invalid")

    return claims
