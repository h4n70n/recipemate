"""Tests for :func:`app.auth.decorators.require_auth` HTTP status semantics.

These focus on the decorator's authentication/authorization contract:

* missing or malformed ``Authorization`` headers return ``401``
* an expired/invalid token (``validate_token`` raising ``AuthError(401)``)
  returns ``401``
* a valid token lacking a required scope returns ``403``
* a valid token carrying the required scope reaches the view (``200``)

``validate_token`` is monkeypatched so the tests never touch Cognito/JWKS;
each test controls exactly which claims the "verified" token yields. User
resolution is likewise stubbed so these tests stay about auth semantics rather
than the database. The app here is a bespoke minimal Flask app (``scopes_app``)
with stubbed routes, intentionally separate from the DB-backed ``app`` fixture
in :mod:`tests.conftest`.
"""

from __future__ import annotations

import pytest
from flask import Flask

from app.auth import AuthError
from app.auth import decorators
from app.auth.decorators import (
    _extract_token_scopes,
    require_auth,
)


@pytest.fixture()
def scopes_app(monkeypatch):
    """A minimal Flask app with scope-protected and bare-protected routes.

    ``validate_token`` is replaced by a stub that decodes a fake "token" of the
    form ``"scope:a,b"`` into claims carrying those scopes, so each request can
    pick the scopes it presents. ``resolve_current_user`` is stubbed to avoid
    the database entirely.
    """

    def fake_validate_token(token: str) -> dict:
        # Real bearer tokens never contain spaces (the header parser would
        # reject them), so the fake token carries scopes comma-delimited:
        #   "sub:<id>|scope:<comma-delimited>"
        # and this stub turns them into the space-delimited ``scope`` claim
        # Cognito actually emits.
        claims: dict = {"sub": "user-1"}
        for part in token.split("|"):
            if part.startswith("scope:"):
                claims["scope"] = part[len("scope:") :].replace(",", " ")
        return claims

    monkeypatch.setattr(decorators, "validate_token", fake_validate_token)
    monkeypatch.setattr(
        decorators, "resolve_current_user", lambda claims: {"sub": claims["sub"]}
    )

    application = Flask(__name__)
    application.config.update(TESTING=True)

    @application.errorhandler(AuthError)
    def _handle_auth_error(error: AuthError):
        return {"error": error.message}, error.status_code

    @application.get("/open")
    @require_auth
    def open_route():
        return {"ok": True}, 200

    @application.get("/write")
    @require_auth(scopes=["recipes:write"])
    def write_route():
        return {"ok": True}, 200

    return application


@pytest.fixture()
def client(scopes_app):
    return scopes_app.test_client()


# ---------------------------------------------------------------------------
# 401 cases
# ---------------------------------------------------------------------------


def test_missing_authorization_header_returns_401(client):
    resp = client.get("/open")
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Authorization header missing"


def test_malformed_authorization_header_returns_401(client):
    resp = client.get("/open", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401

    resp = client.get("/open", headers={"Authorization": "Bearer"})
    assert resp.status_code == 401


def test_invalid_token_returns_401(client, monkeypatch):
    """When validate_token rejects the token, the 401 propagates."""

    def rejecting_validate_token(token: str) -> dict:
        raise AuthError("Authentication token has expired", 401)

    monkeypatch.setattr(decorators, "validate_token", rejecting_validate_token)

    resp = client.get("/open", headers={"Authorization": "Bearer whatever"})
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Authentication token has expired"


# ---------------------------------------------------------------------------
# 403 / scope cases
# ---------------------------------------------------------------------------


def test_valid_token_missing_required_scope_returns_403(client):
    resp = client.get(
        "/write", headers={"Authorization": "Bearer sub:user-1|scope:recipes:read"}
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "Insufficient scope"


def test_valid_token_with_required_scope_returns_200(client):
    resp = client.get(
        "/write",
        headers={"Authorization": "Bearer sub:user-1|scope:recipes:read,recipes:write"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_bare_require_auth_allows_any_valid_token(client):
    """A bare @require_auth route accepts a token with zero scopes."""
    resp = client.get("/open", headers={"Authorization": "Bearer sub:user-1"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# ---------------------------------------------------------------------------
# Scope parsing
# ---------------------------------------------------------------------------


def test_extract_scopes_parses_space_delimited_scope_claim():
    scopes = _extract_token_scopes({"scope": "recipes:read recipes:write"})
    assert scopes == {"recipes:read", "recipes:write"}


def test_extract_scopes_includes_cognito_groups_list():
    scopes = _extract_token_scopes(
        {"scope": "recipes:read", "cognito:groups": ["admins"]}
    )
    assert scopes == {"recipes:read", "admins"}


def test_extract_scopes_empty_when_no_scope_claims():
    assert _extract_token_scopes({"sub": "user-1"}) == set()
