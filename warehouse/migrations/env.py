"""Alembic environment for the Dhaka Kacchi warehouse.

Invariants:
  * DATABASE_URL is the only source of connection truth, and it is REQUIRED.
  * The URL is never written into alembic.ini and never read from
    `sqlalchemy.url` - see the note at the top of alembic.ini.
  * Offline (--sql) and online modes take the same validated URL and the same
    context options, so `alembic upgrade base:head --sql` is a faithful preview.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Make the repo importable no matter where `alembic` was invoked from. This is
# belt-and-braces alongside `prepend_sys_path = %(here)s` in alembic.ini; it is
# what lets version files do `from warehouse.migrations.helpers import ...`.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warehouse.config import load_settings  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# There are no declarative models: warehouse/models/ holds SQL views, not ORM
# classes (ARCHITECTURE.md Appendix A). target_metadata is therefore None and
# --autogenerate / `alembic check` are deliberately unavailable; every migration
# is hand-written and `make verify` is the compensating control. The compare_*
# options below are set correctly in advance in case models ever arrive.
target_metadata = None

SETTINGS = load_settings()

CONTEXT_OPTS = dict(
    target_metadata=target_metadata,
    compare_type=True,
    # Deliberately OFF. This schema is full of server_default=text(...)
    # (gen_random_uuid(), now()); Postgres round-trips those with different
    # whitespace and casts, making server-default comparison a false-positive
    # machine.
    compare_server_default=False,
    # Everything lives in `public`. ARCHITECTURE.md's raw_* landing tables are a
    # table-name prefix, not a schema, so there is nothing to include.
    include_schemas=False,
    version_table="alembic_version",
    version_table_schema=None,
    # Each migration commits on its own. With the default (False) the ENTIRE
    # chain runs in one transaction: a failure at 0009 rolls back 0001-0008 and
    # records nothing, so there is no partial progress to resume from - which
    # defeats the point of making the migration bodies re-runnable. Per-migration
    # transactions also leave room for a future CREATE INDEX CONCURRENTLY step,
    # which cannot exist inside a transaction at all.
    transaction_per_migration=True,
    render_as_batch=False,  # batch mode is a SQLite workaround; never on PG
)


def _assert_server_supports_schema(connection) -> None:
    """Fail fast, once, for the whole chain rather than at migration 0001.

    gen_random_uuid() is only a built-in from PG13; on PG12 and below every UUID
    default in this schema would need pgcrypto.
    """
    version = connection.dialect.server_version_info
    if version is not None and version < (13,):
        raise RuntimeError(
            f"PostgreSQL >= 13 required for native gen_random_uuid(); server is {version}. "
            "Either upgrade, or add create_extension_if_absent('pgcrypto') to migration 0001."
        )


def run_migrations_offline() -> None:
    """Render the chain as static SQL: `alembic upgrade base:head --sql`.

    No connection is opened. Every helper in helpers.py is written to work here -
    which is precisely why none of them introspect the database.
    """
    context.configure(
        url=SETTINGS.sqlalchemy_url.render_as_string(hide_password=False),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **CONTEXT_OPTS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(
        SETTINGS.sqlalchemy_url,
        poolclass=pool.NullPool,  # one short-lived connection; no pool to drain
        echo=SETTINGS.echo_sql,
        # Session GUCs set at connect time via libpq, NOT via `SET` on the
        # connection: Alembic begins and commits transactions on this connection
        # and a transaction-scoped SET would not reliably survive.
        # lock_timeout stops a migration wedging behind someone's open psql.
        connect_args={
            "options": (
                "-c lock_timeout=10s "
                "-c statement_timeout=600s "
                "-c idle_in_transaction_session_timeout=600s"
            )
        },
    )
    try:
        with engine.connect() as connection:
            _assert_server_supports_schema(connection)
            context.configure(connection=connection, **CONTEXT_OPTS)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
