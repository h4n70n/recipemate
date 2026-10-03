"""Shared pytest fixtures for the RecipeMate test suite.

Centralises the pieces every test group needs so individual test modules stay
focused on behaviour:

* :func:`app` -- a Flask app bound to a fresh in-memory SQLite database, with
  the production ``UUID`` columns rendered as ``CHAR(36)`` so ``create_all``
  works without a Postgres driver.
* :func:`client` -- a test client for that app.
* RSA keypair / JWK / token-minting helpers used by the ``validate_token``
  tests to exercise signature and claim verification without any network I/O.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.compiler import compiles

from app import create_app, db


# ---------------------------------------------------------------------------
# SQLite compatibility shim
# ---------------------------------------------------------------------------

# SQLite has no native UUID type. Render the PostgreSQL ``UUID`` column as
# ``CHAR(36)`` for the duration of the test run so ``create_all()`` succeeds
# locally without touching the production model. Registered at import time;
# idempotent because the suite imports this module once.
@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(element, compiler, **kw):  # noqa: ANN001, ANN202
    return "CHAR(36)"


# ---------------------------------------------------------------------------
# App / client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch):
    """A Flask app bound to a fresh in-memory SQLite database.

    The database URI and engine options are set via the config class *before*
    ``create_app`` runs, because Flask-SQLAlchemy builds the engine during
    ``db.init_app`` — overriding the config afterwards would be too late and
    SQLAlchemy would try to import the (absent) Postgres driver.

    Cognito settings are also stubbed so JWT validation has a known issuer,
    region, pool id, and client id to check against.
    """
    monkeypatch.setattr(
        "app.config.Config.SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:"
    )
    # pool_pre_ping / pool_recycle are not valid for SQLite's connection pool.
    monkeypatch.setattr("app.config.Config.SQLALCHEMY_ENGINE_OPTIONS", {})

    # Deterministic Cognito config for token validation tests.
    monkeypatch.setattr("app.config.Config.COGNITO_REGION", "us-east-1")
    monkeypatch.setattr(
        "app.config.Config.COGNITO_USER_POOL_ID", "us-east-1_testpool"
    )
    monkeypatch.setattr(
        "app.config.Config.COGNITO_CLIENT_ID", "test-client-id"
    )

    application = create_app("local")
    application.config.update(TESTING=True)

    with application.app_context():
        # Importing the models package registers Recipe/CookLog so the User
        # relationships resolve and every table is created.
        import app.models  # noqa: F401, PLC0415

        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    """A Flask test client bound to the in-memory SQLite app."""
    return app.test_client()


# ---------------------------------------------------------------------------
# JWT / JWKS helpers (no network)
# ---------------------------------------------------------------------------

# Cognito issuer derived from the stubbed region + pool id above.
TEST_REGION = "us-east-1"
TEST_USER_POOL_ID = "us-east-1_testpool"
TEST_CLIENT_ID = "test-client-id"
TEST_ISSUER = (
    f"https://cognito-idp.{TEST_REGION}.amazonaws.com/{TEST_USER_POOL_ID}"
)
TEST_JWKS_URL = f"{TEST_ISSUER}/.well-known/jwks.json"
TEST_KID = "test-key-1"


@pytest.fixture(scope="session")
def rsa_keypair():
    """Generate a single RSA keypair for the whole test session.

    Returns a ``(private_pem, public_pem)`` tuple. Reused across tests because
    key generation is comparatively slow and the key material never needs to
    differ between cases.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


@pytest.fixture()
def public_jwk(rsa_keypair):
    """Build a JWK dict (with ``kid``) from the session public key.

    This is the shape Cognito's JWKS endpoint returns and what
    ``validate_token`` resolves the signing key from.
    """
    from jose import jwk

    _, public_pem = rsa_keypair
    key_obj = jwk.construct(public_pem, "RS256")
    jwk_dict = key_obj.to_dict()
    # ``to_dict`` returns bytes for some fields depending on backend; normalise
    # to JSON-safe strings and attach the key id the token header references.
    jwk_dict = json.loads(json.dumps(jwk_dict, default=_bytes_to_str))
    jwk_dict["kid"] = TEST_KID
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    return jwk_dict


def _bytes_to_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    raise TypeError(f"Not JSON serialisable: {type(value)!r}")


@pytest.fixture()
def rsa_private_pem(rsa_keypair):
    """Expose just the session private key PEM for ad-hoc token minting."""
    private_pem, _ = rsa_keypair
    return private_pem


@pytest.fixture()
def make_token(rsa_keypair):
    """Return a helper that mints RS256-signed JWTs with the test key.

    The returned callable accepts arbitrary claim overrides and signs the
    resulting payload with ``kid=TEST_KID`` so ``validate_token`` can resolve
    it against the primed JWKS cache.
    """
    from jose import jwt

    private_pem, _ = rsa_keypair

    def _make(**claims: Any) -> str:
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        payload: dict[str, Any] = {
            "sub": "cognito-sub-123",
            "iss": TEST_ISSUER,
            "iat": int(now.timestamp()),
            "exp": int((now + datetime.timedelta(hours=1)).timestamp()),
        }
        payload.update(claims)
        # Allow explicitly removing a claim by passing it as ``None``.
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(
            payload,
            private_pem,
            algorithm="RS256",
            headers={"kid": TEST_KID},
        )

    return _make


@pytest.fixture()
def prime_jwks_cache(public_jwk):
    """Populate ``app.auth.jwt._JWKS_CACHE`` with the local public key.

    Keyed by the JWKS URL so ``get_jwks`` / ``_find_signing_key`` resolve the
    test key without a network request. The cache is cleared afterwards so one
    test's priming never leaks into another.
    """
    from app.auth import jwt as jwt_module

    jwt_module._JWKS_CACHE[TEST_JWKS_URL] = {TEST_KID: public_jwk}
    yield jwt_module._JWKS_CACHE
    jwt_module._JWKS_CACHE.clear()
