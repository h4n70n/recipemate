"""Direct tests for :func:`app.auth.jwt.validate_token`.

These exercise the signature + claim verification path without any network
access. The module-level JWKS cache (``app.auth.jwt._JWKS_CACHE``, keyed by the
JWKS URL) is pre-populated with a locally generated RS256 public key via the
``prime_jwks_cache`` fixture, and tokens are minted with the matching private
key via the ``make_token`` helper (both defined in :mod:`tests.conftest`).

The ``app`` fixture supplies the application context ``validate_token`` needs to
read Cognito config, and stubs the issuer/region/pool/client values the tokens
are built against.
"""

from __future__ import annotations

import datetime

import pytest
from jose import jwt as jose_jwt

from app.auth import jwt as jwt_module
from app.auth.jwt import AuthError, validate_token
from tests.conftest import (
    TEST_CLIENT_ID,
    TEST_ISSUER,
    TEST_JWKS_URL,
    TEST_KID,
)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_valid_id_token_returns_claims(app, prime_jwks_cache, make_token):
    """An ID token (aud == client id, correct iss) validates and returns claims."""
    token = make_token(aud=TEST_CLIENT_ID, email="chef@example.com")

    with app.app_context():
        claims = validate_token(token)

    assert claims["sub"] == "cognito-sub-123"
    assert claims["aud"] == TEST_CLIENT_ID
    assert claims["iss"] == TEST_ISSUER
    assert claims["email"] == "chef@example.com"


def test_valid_access_token_returns_claims(app, prime_jwks_cache, make_token):
    """An access token (no aud, client_id == client id) validates and returns claims."""
    # Access tokens omit ``aud`` and carry the client id in ``client_id``.
    token = make_token(aud=None, client_id=TEST_CLIENT_ID, scope="recipes:read")

    with app.app_context():
        claims = validate_token(token)

    assert claims["sub"] == "cognito-sub-123"
    assert "aud" not in claims
    assert claims["client_id"] == TEST_CLIENT_ID
    assert claims["scope"] == "recipes:read"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_expired_token_raises_auth_error(app, prime_jwks_cache, make_token):
    """A token whose ``exp`` is in the past is rejected."""
    past = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(
        hours=2
    )
    token = make_token(
        aud=TEST_CLIENT_ID,
        exp=int(past.timestamp()),
        iat=int((past - datetime.timedelta(hours=1)).timestamp()),
    )

    with app.app_context():
        with pytest.raises(AuthError) as exc_info:
            validate_token(token)

    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.message.lower()


def test_wrong_audience_raises_auth_error(app, prime_jwks_cache, make_token):
    """An ID token whose ``aud`` is not the configured client id is rejected."""
    token = make_token(aud="some-other-client")

    with app.app_context():
        with pytest.raises(AuthError) as exc_info:
            validate_token(token)

    assert exc_info.value.status_code == 401


def test_wrong_access_token_client_id_raises_auth_error(
    app, prime_jwks_cache, make_token
):
    """An access token whose ``client_id`` is not the configured one is rejected."""
    token = make_token(aud=None, client_id="some-other-client")

    with app.app_context():
        with pytest.raises(AuthError) as exc_info:
            validate_token(token)

    assert exc_info.value.status_code == 401
    assert "audience" in exc_info.value.message.lower()


def test_wrong_issuer_raises_auth_error(app, prime_jwks_cache, make_token):
    """A token signed with the wrong ``iss`` is rejected."""
    token = make_token(
        aud=TEST_CLIENT_ID,
        iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_evilpool",
    )

    with app.app_context():
        with pytest.raises(AuthError) as exc_info:
            validate_token(token)

    assert exc_info.value.status_code == 401


def test_unknown_kid_attempts_refresh_then_raises(
    app, prime_jwks_cache, rsa_private_pem, monkeypatch
):
    """A token with an unknown ``kid`` triggers a cache refresh before failing.

    The primed cache only knows ``TEST_KID``. We sign an otherwise-valid token
    with a different kid, then assert ``validate_token`` attempted a forced
    JWKS refresh (short-circuited here to avoid the network) before raising.
    """
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    token = jose_jwt.encode(
        {
            "sub": "cognito-sub-123",
            "iss": TEST_ISSUER,
            "aud": TEST_CLIENT_ID,
            "iat": int(now.timestamp()),
            "exp": int((now + datetime.timedelta(hours=1)).timestamp()),
        },
        rsa_private_pem,
        algorithm="RS256",
        headers={"kid": "unknown-kid"},
    )

    refresh_calls: list[bool] = []

    def tracking_get_jwks(*, force_refresh: bool = False):
        refresh_calls.append(force_refresh)
        # Short-circuit the network: return the primed set (which still lacks
        # the unknown kid) instead of hitting Cognito.
        return prime_jwks_cache[TEST_JWKS_URL]

    monkeypatch.setattr(jwt_module, "get_jwks", tracking_get_jwks)

    with app.app_context():
        with pytest.raises(AuthError) as exc_info:
            validate_token(token)

    assert exc_info.value.status_code == 401
    # A forced refresh must have been attempted when the kid was not found.
    assert True in refresh_calls, "expected a forced JWKS refresh on unknown kid"


def test_malformed_token_raises_auth_error(app, prime_jwks_cache):
    """A token that is not a well-formed JWT is rejected."""
    with app.app_context():
        with pytest.raises(AuthError) as exc_info:
            validate_token("this-is-not-a-jwt")

    assert exc_info.value.status_code == 401
    assert "malformed" in exc_info.value.message.lower()


def test_empty_token_raises_auth_error(app, prime_jwks_cache):
    """An empty token string is rejected before any JWKS work."""
    with app.app_context():
        with pytest.raises(AuthError):
            validate_token("")


def test_known_kid_resolves_without_refresh(
    app, prime_jwks_cache, make_token, monkeypatch
):
    """A token whose kid is already cached does not force a JWKS refresh."""
    token = make_token(aud=TEST_CLIENT_ID)

    assert TEST_KID in prime_jwks_cache[TEST_JWKS_URL]

    refresh_calls: list[bool] = []
    original_get_jwks = jwt_module.get_jwks

    def tracking_get_jwks(*, force_refresh: bool = False):
        refresh_calls.append(force_refresh)
        return original_get_jwks(force_refresh=force_refresh)

    monkeypatch.setattr(jwt_module, "get_jwks", tracking_get_jwks)

    with app.app_context():
        claims = validate_token(token)

    assert claims["sub"] == "cognito-sub-123"
    assert refresh_calls == [False], "cached kid should not trigger a refresh"
