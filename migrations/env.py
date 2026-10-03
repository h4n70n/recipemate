"""Alembic environment configuration for RecipeMate.

This module is executed by Alembic during every ``alembic`` command.  It
wires together:

* The SQLAlchemy ``MetaData`` object (via the shared ``db`` instance) so that
  ``--autogenerate`` can diff models against the live schema.
* The ``DATABASE_URL`` environment variable as the connection string, matching
  the value that Flask uses at runtime.
* Both *offline* (SQL-script) and *online* (live connection) migration modes.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Alembic Config object — gives access to alembic.ini values
# ---------------------------------------------------------------------------
config = context.config

# Configure Python logging from the ini file (if present).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Import the application's SQLAlchemy instance so Alembic can inspect the
# model metadata for autogenerate support.
# ---------------------------------------------------------------------------
from app import db  # noqa: E402 — must come after sys.path is set by alembic.ini

target_metadata = db.metadata

# ---------------------------------------------------------------------------
# Override the SQLAlchemy URL with the DATABASE_URL environment variable so
# migrations always target the same database as the running application.
# ---------------------------------------------------------------------------
_database_url = os.environ.get("DATABASE_URL")
if _database_url:
    config.set_main_option("sqlalchemy.url", _database_url)


# ---------------------------------------------------------------------------
# Offline migration mode
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Run migrations without a live database connection.

    Emits the migration SQL to stdout (or a file) rather than executing it
    directly.  Useful for reviewing or applying migrations manually.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Emit ``COMMIT`` between each migration step.
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migration mode
# ---------------------------------------------------------------------------

def run_migrations_online() -> None:
    """Run migrations against a live database connection.

    Creates an engine from the config, acquires a connection, and runs all
    pending migration steps inside a transaction.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Compare server defaults so autogenerate catches DEFAULT changes.
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
