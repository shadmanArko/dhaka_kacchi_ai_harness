"""The single accessor for process configuration.

House rule, mirrored from the sibling TypeScript services: nothing outside this
module reads os.environ. Everything takes a Settings object. Validation is
fail-fast at load time, not at first query, and DATABASE_URL has no default.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"

_BACKEND = "postgresql"
_DRIVER = "postgresql+psycopg"
_MAINTENANCE_DBS = frozenset({"postgres", "template0", "template1"})
_LOCAL_HOSTS = frozenset({None, "", "localhost", "127.0.0.1", "::1"})


class ConfigError(RuntimeError):
    """The environment is unusable. Never caught - this aborts the process."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: URL  # driver-agnostic postgresql://...  (safe to hand to psql)
    sqlalchemy_url: URL  # postgresql+psycopg://...        (for create_engine)
    admin_url: URL  # same host/role, database='postgres'  (for CREATE DATABASE)
    database_name: str
    echo_sql: bool


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read and validate configuration.

    Raises ConfigError with an actionable message; never returns a partially
    valid object. Pass `environ` explicitly in tests. In normal use the real
    process environment wins over .env (override=False), matching the sibling
    convention that the process environment is truth and .env is a dev fallback.
    """
    if environ is None:
        load_dotenv(ENV_FILE, override=False)
        environ = os.environ

    raw = (environ.get("DATABASE_URL") or "").strip()
    if not raw:
        raise ConfigError(
            "DATABASE_URL is required and has no default.\n"
            "  Set it in the process environment, or run `make env` to create "
            f"{ENV_FILE} from .env.example."
        )

    if raw.startswith("postgres://"):
        raise ConfigError("DATABASE_URL must use the 'postgresql://' scheme, not 'postgres://'.")

    try:
        url = make_url(raw)
    except Exception as exc:
        raise ConfigError(f"DATABASE_URL is not a valid URL: {exc}") from None

    if url.get_backend_name() != _BACKEND:
        raise ConfigError(f"DATABASE_URL must be a PostgreSQL URL; got driver {url.drivername!r}.")
    if not url.database:
        raise ConfigError(
            "DATABASE_URL must name a database, e.g. postgresql://user@127.0.0.1:5432/dhaka_kacchi"
        )
    if url.database in _MAINTENANCE_DBS:
        raise ConfigError(
            f"refusing to point the warehouse at maintenance database {url.database!r}."
        )
    if url.host not in _LOCAL_HOSTS and url.query.get("sslmode") is None:
        raise ConfigError(
            "DATABASE_URL points at a non-local host but does not set sslmode. "
            "Append ?sslmode=require."
        )

    # A bare postgresql:// URL makes SQLAlchemy 2.0 reach for psycopg2, which is
    # not installed. Normalising here is what lets DATABASE_URL stay
    # driver-agnostic and directly consumable by psql and the ingest jobs.
    sqlalchemy_url = url.set(drivername=_DRIVER)

    return Settings(
        database_url=url,
        sqlalchemy_url=sqlalchemy_url,
        admin_url=sqlalchemy_url.set(database="postgres"),
        database_name=url.database,
        echo_sql=(environ.get("WAREHOUSE_ECHO_SQL", "").lower() in {"1", "true", "yes"}),
    )


def _main(argv: list[str]) -> int:
    """`python -m warehouse.config [print-url]` - used by `make psql`."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if argv[1:2] == ["print-url"]:
        print(settings.database_url.render_as_string(hide_password=False))
        return 0
    print(f"database : {settings.database_name}")
    print(f"host     : {settings.database_url.host}:{settings.database_url.port}")
    print(f"user     : {settings.database_url.username}")
    print(f"driver   : {settings.sqlalchemy_url.drivername}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
