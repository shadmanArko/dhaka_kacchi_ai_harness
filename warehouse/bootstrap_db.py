"""Create the warehouse database if it is not already there.

`alembic upgrade head` cannot create its own database, so this runs ahead of
every migrate target. Safe to run any number of times.

Implemented in Python rather than `createdb` in the Makefile so the database
name, host, port and role come from the one config accessor instead of being
hardcoded a second time and drifting from DATABASE_URL.
"""

from __future__ import annotations

import argparse
import sys

import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError

from warehouse.config import ConfigError, Settings, load_settings

DUPLICATE_DATABASE = "42P04"  # SQLSTATE


def _q(identifier: str) -> str:
    """Quote a Postgres identifier. Identifiers cannot be bind parameters."""
    return '"' + identifier.replace('"', '""') + '"'


def _admin_engine(settings: Settings) -> sa.Engine:
    # CREATE DATABASE / DROP DATABASE cannot run inside a transaction block, and
    # SQLAlchemy 2.0 opens one implicitly on every connection. AUTOCOMMIT is
    # mandatory here, not a style choice.
    return sa.create_engine(
        settings.admin_url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool
    )


def create(settings: Settings) -> int:
    name = settings.database_name
    with _admin_engine(settings).connect() as conn:
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).scalar()
        if exists:
            print(f"database {name!r} already exists")
            return 0
        try:
            # TEMPLATE template1 inherits this cluster's UTF8 / LC_COLLATE=C.
            conn.execute(sa.text(f"CREATE DATABASE {_q(name)} TEMPLATE template1 ENCODING 'UTF8'"))
        except ProgrammingError as exc:
            # check-then-create is not atomic; a concurrent createdb is success,
            # not failure. This is what makes it genuinely idempotent rather
            # than merely usually-idempotent.
            if getattr(exc.orig, "sqlstate", None) == DUPLICATE_DATABASE:
                print(f"database {name!r} was created concurrently")
                return 0
            raise
    print(f"created database {name!r}")
    return 0


def drop(settings: Settings, *, yes: bool) -> int:
    name = settings.database_name
    if not yes:
        reply = input(f"drop database {name!r} and everything in it? type the name to confirm: ")
        if reply.strip() != name:
            print("aborted")
            return 1
    with _admin_engine(settings).connect() as conn:
        conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": name},
        )
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {_q(name)}"))
    print(f"dropped database {name!r}")
    return 0


def forget_migrations(settings: Settings) -> int:
    """Dev-only. Empty alembic_version so the whole chain replays against a
    schema that already exists.

    This is the ONLY real test of the IF NOT EXISTS guards: running
    `alembic upgrade head` twice proves nothing, because the version table
    short-circuits the second run. See `make verify-idempotent`.
    """
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    with engine.begin() as conn:
        if conn.execute(sa.text("SELECT to_regclass('public.alembic_version')")).scalar():
            conn.execute(sa.text("DELETE FROM alembic_version"))
    print("cleared alembic_version; `alembic upgrade head` will replay the full chain")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the warehouse database.")
    parser.add_argument("--drop", action="store_true", help="DROP DATABASE")
    parser.add_argument(
        "--forget-migrations", action="store_true", help="dev-only: empty alembic_version"
    )
    parser.add_argument("--yes", action="store_true", help="skip the drop confirmation")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.drop:
        return drop(settings, yes=args.yes)
    if args.forget_migrations:
        return forget_migrations(settings)
    return create(settings)


if __name__ == "__main__":
    raise SystemExit(main())
