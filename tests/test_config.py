"""Tests for environment-based configuration (TASK-8.3).

Covers the two behaviours TASK-8.3 adds to :mod:`app.config`:

* ``SQLALCHEMY_DATABASE_URI`` is composed from the individual RDS secret parts
  (``DB_USER``/``DB_PASSWORD``/``DB_HOST``/``DB_PORT``/``DB_NAME``) that
  ``ApiStack`` injects as container secrets in staging/prod, with the password
  URL-encoded — falling back to a directly supplied ``DATABASE_URL`` for the
  local/dev path so Docker Compose keeps working unchanged.
* Staging and prod **fail closed**: :func:`app.config.get_config` raises if
  ``SECRET_KEY`` is unset or still the insecure ``change-me-in-production``
  default, while ``local`` keeps the permissive default.

The module reads its values at import time, so these tests set the environment
and reload :mod:`app.config` inside each case rather than relying on import
order. The reload is undone in a fixture so one test never leaks config state
into another.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def fresh_config(monkeypatch):
    """Return a callable that reloads ``app.config`` under the current env.

    All of the ``SECRET_KEY``/``DB_*``/``DATABASE_URL``/``FLASK_ENV`` env vars
    are cleared first so each case starts from a known baseline; the caller
    sets what it needs via ``monkeypatch`` before invoking the reload. The
    module is reloaded once more at teardown so later tests see a clean import.
    """
    for var in (
        "SECRET_KEY",
        "DATABASE_URL",
        "DB_USER",
        "DB_PASSWORD",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "FLASK_ENV",
    ):
        monkeypatch.delenv(var, raising=False)

    def _reload():
        import app.config as config_module

        return importlib.reload(config_module)

    yield _reload

    import app.config as config_module

    importlib.reload(config_module)


# ---------------------------------------------------------------------------
# SQLALCHEMY_DATABASE_URI composition
# ---------------------------------------------------------------------------


def test_database_uri_composed_from_rds_parts(monkeypatch, fresh_config):
    """DB_* parts compose a psycopg2 URL; the password is URL-encoded."""
    monkeypatch.setenv("DB_USER", "recipemate_admin")
    monkeypatch.setenv("DB_PASSWORD", "p@ss/w:rd")  # reserved chars
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "recipemate")

    config_module = fresh_config()

    assert config_module.Config.SQLALCHEMY_DATABASE_URI == (
        "postgresql+psycopg2://recipemate_admin:p%40ss%2Fw%3Ard"
        "@db.internal:5432/recipemate"
    )


def test_database_uri_falls_back_to_database_url(monkeypatch, fresh_config):
    """Without DB_* parts, a directly supplied DATABASE_URL is used (local)."""
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://recipemate:recipemate@db:5432/recipemate",
    )

    config_module = fresh_config()

    assert config_module.Config.SQLALCHEMY_DATABASE_URI == (
        "postgresql+psycopg2://recipemate:recipemate@db:5432/recipemate"
    )


def test_partial_db_parts_fall_back_to_database_url(monkeypatch, fresh_config):
    """A partial DB_* set does not compose a URL; DATABASE_URL still wins."""
    monkeypatch.setenv("DB_USER", "recipemate_admin")
    monkeypatch.setenv("DB_HOST", "db.internal")
    # DB_PASSWORD / DB_NAME intentionally absent.
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg2://local:local@localhost:5432/x"
    )

    config_module = fresh_config()

    assert config_module.Config.SQLALCHEMY_DATABASE_URI == (
        "postgresql+psycopg2://local:local@localhost:5432/x"
    )


# ---------------------------------------------------------------------------
# SECRET_KEY fail-closed in staging/prod
# ---------------------------------------------------------------------------


def test_local_allows_insecure_default(monkeypatch, fresh_config):
    """Local keeps the permissive placeholder SECRET_KEY and never raises."""
    config_module = fresh_config()

    cfg = config_module.get_config("local")
    assert cfg.SECRET_KEY == config_module._INSECURE_SECRET_KEY_DEFAULT


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_deployed_rejects_default_secret_key(monkeypatch, fresh_config, env):
    """Staging/prod refuse to start on the insecure default SECRET_KEY."""
    config_module = fresh_config()

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        config_module.get_config(env)


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_deployed_accepts_real_secret_key(monkeypatch, fresh_config, env):
    """A real injected SECRET_KEY lets staging/prod start."""
    monkeypatch.setenv("SECRET_KEY", "a-real-long-random-signing-key")

    config_module = fresh_config()

    cfg = config_module.get_config(env)
    assert cfg.SECRET_KEY == "a-real-long-random-signing-key"
