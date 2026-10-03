"""Tests for lazy user provisioning in :mod:`app.auth.decorators`.

These exercise :func:`resolve_current_user` / :func:`_provision_user` end to
end against a real (in-memory SQLite) database, so we verify actual INSERT and
unique-constraint behaviour rather than mocking the session.

The in-memory SQLite ``app`` fixture (and the UUID→CHAR(36) compiler shim that
makes ``create_all`` work locally) lives in :mod:`tests.conftest`.
"""

from __future__ import annotations

import uuid


def _claims(sub: str, **extra) -> dict:
    return {"sub": sub, **extra}


def test_first_request_creates_user(app):
    """An unknown cognito_sub is lazily provisioned on first resolution."""
    from app.auth.decorators import resolve_current_user
    from app.models.user import User

    with app.app_context():
        assert User.query.count() == 0

        user = resolve_current_user(_claims("sub-123", email="chef@example.com"))

        assert user.id is not None
        assert isinstance(user.id, uuid.UUID)
        assert user.cognito_sub == "sub-123"
        assert user.email == "chef@example.com"
        assert User.query.count() == 1


def test_second_request_returns_same_user(app):
    """A repeat request for a known subject returns the existing row, no insert."""
    from app.auth.decorators import resolve_current_user
    from app.models.user import User

    with app.app_context():
        first = resolve_current_user(_claims("sub-abc", email="a@example.com"))
        second = resolve_current_user(_claims("sub-abc", email="a@example.com"))

        assert first.id == second.id
        assert User.query.count() == 1


def test_access_token_without_email_uses_placeholder(app):
    """When no email claim is present, a deterministic placeholder is used."""
    from app.auth.decorators import resolve_current_user

    with app.app_context():
        user = resolve_current_user(_claims("sub-no-email"))

        assert user.email == "sub-no-email@users.noreply.recipemate"
        assert user.cognito_sub == "sub-no-email"


def test_access_token_falls_back_to_username(app):
    """A username claim is preferred over the generated placeholder."""
    from app.auth.decorators import resolve_current_user

    with app.app_context():
        user = resolve_current_user(_claims("sub-user", username="pistachio"))

        assert user.email == "pistachio"


def test_concurrent_creation_race_is_handled(app, monkeypatch):
    """If a competing request inserts the same subject mid-flight, we return it.

    Simulates the race by inserting and committing a row for the same subject
    just before our own commit, which then raises IntegrityError on the unique
    ``cognito_sub`` constraint. _provision_user must roll back and return the
    row the "other request" committed.
    """
    from app import db
    from app.auth import decorators
    from app.models.user import User

    with app.app_context():
        original_commit = db.session.commit
        state = {"raced": False}

        def racing_commit():
            # On the first commit attempt, a concurrent request has already
            # committed the same cognito_sub via a separate session.
            if not state["raced"]:
                state["raced"] = True
                engine = db.engine
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "INSERT INTO users (id, email, cognito_sub, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            "winner@example.com",
                            "sub-race",
                            "2024-01-01 00:00:00",
                        ),
                    )
            return original_commit()

        monkeypatch.setattr(db.session, "commit", racing_commit)

        user = decorators.resolve_current_user(
            _claims("sub-race", email="loser@example.com")
        )

        # We got back the row the competing request committed, not a duplicate.
        assert user.cognito_sub == "sub-race"
        assert user.email == "winner@example.com"
        assert User.query.filter_by(cognito_sub="sub-race").count() == 1
