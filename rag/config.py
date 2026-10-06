"""The single accessor for RAG-subsystem configuration.

House rule, mirrored from warehouse/config.py: nothing outside this module
reads os.environ for anything RAG-related. Everything takes a settings
object. Validation is fail-fast at load time, not at first query.

Three separate loaders, not one combined Settings object (see
rag/RAG_progress.md decision #8): each consumer gets exactly the credential
it needs and no others.

  load_rag_admin_settings()   - one-time setup only (CREATE DATABASE,
                                 CREATE EXTENSION vector, role creation).
                                 Never imported by ingestion or agent code.
  load_rag_writer_settings()  - the recurring chunk-ingestion job. Can
                                 INSERT/UPDATE the chunks table, nothing
                                 more.
  load_rag_reader_settings()  - agent query code at retrieval time.
                                 Read-only, no write grants at all.
"""

# This makes type hints like `URL | None` work even on older Python
# versions that don't natively support that syntax - it tells Python to
# treat all type annotations as text instead of evaluating them right away.
from __future__ import annotations

# The standard library module for reading environment variables
# (os.environ) - this is the ONLY file in rag/ allowed to touch it, per
# the house rule stated in the docstring above.
import os

# Mapping is a generic "read-only dictionary-like object" type, used here
# so this module can accept either the real os.environ or a plain dict
# (e.g. in tests) without caring which.
from collections.abc import Mapping

# `dataclass` auto-generates the boring parts of a class (the constructor,
# equality checks, a repr) from just a list of fields - used below for the
# three settings objects, so each one is just "here are its fields", not
# hand-written boilerplate.
from dataclasses import dataclass

# `Path` gives us an object-oriented, OS-independent way to build file
# paths (works the same on Windows and Linux), instead of gluing strings
# together with slashes.
from pathlib import Path

# `load_dotenv` reads a `.env` file and copies its key=value lines into
# the process environment, as a convenience for local development (so you
# don't have to `export` every variable by hand in your terminal).
from dotenv import load_dotenv

# `URL` is SQLAlchemy's structured representation of a database connection
# string (scheme, user, password, host, port, database, query params) -
# safer to pass around than a raw string, because it validates its own
# shape. `make_url` parses a raw string into that structured object.
from sqlalchemy.engine import URL, make_url

# Walk up from this file's own location (rag/config.py) two levels:
# parents[0] is the rag/ folder itself, parents[1] is the repository root.
# This gives us an absolute path to the repo root regardless of which
# directory the process was started from.
REPO_ROOT = Path(__file__).resolve().parents[1]

# The expected location of the local `.env` file: directly in the repo
# root, next to warehouse/'s own .env usage, so both subsystems share one
# file.
ENV_FILE = REPO_ROOT / ".env"

# The only database backend this project ever connects to - used below to
# reject any URL that claims to be, say, MySQL or SQLite.
_BACKEND = "postgresql"

# The specific SQLAlchemy "driver" string that tells SQLAlchemy to use the
# `psycopg` (version 3) library under the hood, rather than defaulting to
# the older `psycopg2`, which isn't installed in this project.
_DRIVER = "postgresql+psycopg"

# Postgres ships with these built-in "maintenance" databases that always
# exist on any server. You're never supposed to store real application
# data in them - they exist so you have somewhere to connect *before* your
# own database has been created yet (see RagAdminSettings below, which
# deliberately points here).
_MAINTENANCE_DBS = frozenset({"postgres", "template0", "template1"})

# The set of hostnames that count as "this same machine" - used to decide
# whether a connection needs to enforce encryption (sslmode). None/"" is
# included because some URL forms omit the host entirely when it's local.
_LOCAL_HOSTS = frozenset({None, "", "localhost", "127.0.0.1", "::1"})


class ConfigError(RuntimeError):
    """The environment is unusable. Never caught - this aborts the process."""

    # (No body needed beyond the docstring - this class exists purely so
    # callers can `raise ConfigError(...)` with a specific, recognisable
    # error type instead of a generic RuntimeError.)


def _validate_postgres_url(raw: str, *, var_name: str, allow_maintenance: bool) -> URL:
    """Shape rules shared by every RAG Postgres connection string.

    Same rules as warehouse/config.py's helper of the same name (must be
    postgresql://, must parse, must set sslmode when the host isn't local),
    plus one RAG-specific parameter: the admin loader connects to the
    `postgres` maintenance database on purpose (dhaka_kacchi_rag doesn't
    exist yet the first time it runs), so it needs the maintenance-db check
    to be skippable - unlike warehouse/config.py's version, which always
    rejects it because every one of its callers targets a real, already-
    created database.
    """
    # `postgres://` is an older, deprecated alias for the same scheme;
    # SQLAlchemy 2.0 no longer accepts it, so we catch this early and tell
    # the user exactly what to change, rather than letting a confusing
    # error surface later from deep inside SQLAlchemy.
    if raw.startswith("postgres://"):
        raise ConfigError(f"{var_name} must use the 'postgresql://' scheme, not 'postgres://'.")

    # Try to parse the raw string into a structured URL object. If it's
    # malformed (missing parts, bad characters, etc.), `make_url` raises -
    # we catch that generic exception and re-raise our own ConfigError so
    # every failure in this module looks the same to callers.
    try:
        url = make_url(raw)
    except Exception as exc:
        raise ConfigError(f"{var_name} is not a valid URL: {exc}") from None

    # Reject anything that isn't Postgres (e.g. someone accidentally pastes
    # a MySQL or SQLite URL here).
    if url.get_backend_name() != _BACKEND:
        raise ConfigError(f"{var_name} must be a PostgreSQL URL; got driver {url.drivername!r}.")

    # A URL with no database name at all (e.g. just "postgresql://host/")
    # can't be used to connect to anything specific - fail with a helpful
    # example rather than letting a downstream connection attempt fail
    # with a cryptic error.
    if not url.database:
        raise ConfigError(
            f"{var_name} must name a database, e.g. postgresql://user@127.0.0.1:5432/dbname"
        )

    # Unless this specific call explicitly allowed it (only the admin
    # loader does), refuse to let anyone accidentally point a writer or
    # reader connection at a maintenance database - that would almost
    # certainly be a copy-paste mistake, not an intentional choice.
    if not allow_maintenance and url.database in _MAINTENANCE_DBS:
        raise ConfigError(f"refusing to use maintenance database {url.database!r} for {var_name}.")

    # If the target host is NOT this local machine, force the connection
    # string to explicitly opt into (or out of) TLS via `sslmode` - this
    # prevents silently sending credentials/data over an unencrypted
    # connection to a remote server just because nobody thought to add the
    # query parameter.
    if url.host not in _LOCAL_HOSTS and url.query.get("sslmode") is None:
        raise ConfigError(
            f"{var_name} points at a non-local host but does not set sslmode. "
            "Append ?sslmode=require (or ?sslmode=disable for container-to-container "
            "traffic on the same Docker network - see the VPS infrastructure docs)."
        )

    # Everything checked out - hand back the structured, validated URL.
    return url


def _load_environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    # This helper exists so every loader function below can either (a)
    # take an explicit dict of fake environment variables, useful for
    # tests, or (b) fall through to the real process environment for
    # normal use - without duplicating this branching logic three times.
    if environ is not None:
        # A caller (e.g. a test) already handed us the environment to use -
        # use it exactly as given, and don't touch the real .env file.
        return environ
    # No explicit environment was given, so this is a real run: load any
    # values from the local .env file into the process environment first
    # (without overwriting anything already set for real, since
    # override=False), then read from the real environment.
    load_dotenv(ENV_FILE, override=False)
    return os.environ


@dataclass(frozen=True, slots=True)
class RagAdminSettings:
    """Config for one-time RAG database setup: CREATE DATABASE, CREATE
    EXTENSION vector, and creation of the rag_writer / rag_reader roles.

    Points at the `postgres` maintenance database - dhaka_kacchi_rag does
    not exist yet the first time this runs, so there is nothing else to
    connect to. Never used by the ingestion job or by agent code; import
    this only from the one-off setup script.
    """

    # The single field this settings object carries: a ready-to-use
    # SQLAlchemy connection URL, already pointed at the `postgresql+psycopg`
    # driver, for whoever runs the one-time setup.
    sqlalchemy_url: URL


def load_rag_admin_settings(environ: Mapping[str, str] | None = None) -> RagAdminSettings:
    """Fail-fast config for the RAG database setup script."""
    # Get either the real process environment or the explicit one passed
    # in (see _load_environ's own comments above).
    environ = _load_environ(environ)

    # Look up the raw connection string for the admin role. `.get(...)`
    # returns None if it's missing, so `or ""` turns that into an empty
    # string, and `.strip()` removes any accidental leading/trailing
    # whitespace a human might paste in.
    raw = (environ.get("RAG_ADMIN_DATABASE_URL") or "").strip()

    # If nothing was set at all, fail immediately with a message that
    # tells the reader exactly what to do about it, instead of letting a
    # blank string cause a confusing error several steps later.
    if not raw:
        raise ConfigError(
            "RAG_ADMIN_DATABASE_URL is required and has no default.\n"
            "  Set it in the process environment, or add it to "
            f"{ENV_FILE} (see .env.example). Must point at the 'postgres' "
            "maintenance database with a role that can CREATE DATABASE, "
            "CREATE EXTENSION, and CREATE ROLE."
        )

    # Run the shared validation rules, explicitly allowing this one to
    # target a maintenance database (see that parameter's own comment
    # above for why the admin case is the exception).
    url = _validate_postgres_url(raw, var_name="RAG_ADMIN_DATABASE_URL", allow_maintenance=True)

    # `.set(drivername=_DRIVER)` returns a new URL object with just the
    # driver portion swapped to "postgresql+psycopg" (the original might
    # have been the more generic, driver-agnostic "postgresql"), then we
    # wrap it in the dataclass and hand it back to the caller.
    return RagAdminSettings(sqlalchemy_url=url.set(drivername=_DRIVER))


@dataclass(frozen=True, slots=True)
class RagWriterSettings:
    """Config for the recurring chunk-ingestion job (chunk -> embed ->
    upsert into the chunks table). Connects as the rag_writer role, which
    can INSERT/UPDATE the chunks table and nothing else - no DDL, no access
    to any other database on the instance.
    """

    # Same single-field shape as RagAdminSettings above, but this URL is
    # meant to be used by the ingestion job, connecting as the more
    # restricted rag_writer role instead of an admin role.
    sqlalchemy_url: URL


def load_rag_writer_settings(environ: Mapping[str, str] | None = None) -> RagWriterSettings:
    """Fail-fast config for rag/ingest.py (or wherever the ingestion job
    ends up living).
    """
    # Same environment-resolution step as every other loader in this file.
    environ = _load_environ(environ)

    # Read and clean up the writer-specific connection string.
    raw = (environ.get("RAG_WRITER_DATABASE_URL") or "").strip()

    # Same fail-fast pattern as the admin loader: if it's missing, stop
    # here with an actionable message rather than limping forward.
    if not raw:
        raise ConfigError(
            "RAG_WRITER_DATABASE_URL is required and has no default.\n"
            "  Set it in the process environment, or add it to "
            f"{ENV_FILE} (see .env.example). Must point at dhaka_kacchi_rag "
            "as the rag_writer role."
        )

    # Validate it with the maintenance-database check turned back ON
    # (allow_maintenance=False) - a writer connection has no legitimate
    # reason to ever point at the `postgres` database.
    url = _validate_postgres_url(raw, var_name="RAG_WRITER_DATABASE_URL", allow_maintenance=False)

    # Normalise the driver the same way as the admin loader, then return
    # the wrapped result.
    return RagWriterSettings(sqlalchemy_url=url.set(drivername=_DRIVER))


@dataclass(frozen=True, slots=True)
class RagReaderSettings:
    """Config for agent query code at retrieval time. Connects as the
    rag_reader role: read-only, no write grants at all, structurally
    incapable of mutating chunks or anything else in dhaka_kacchi_rag.
    """

    # Same shape again: one validated, driver-normalised connection URL,
    # this time meant for the read-only rag_reader role that agents use.
    sqlalchemy_url: URL


def load_rag_reader_settings(environ: Mapping[str, str] | None = None) -> RagReaderSettings:
    """Fail-fast config for agent-facing retrieval code."""
    # Same environment-resolution step as every other loader.
    environ = _load_environ(environ)

    # Read and clean up the reader-specific connection string.
    raw = (environ.get("RAG_READER_DATABASE_URL") or "").strip()

    # Same fail-fast pattern once more: no silent defaults, ever.
    if not raw:
        raise ConfigError(
            "RAG_READER_DATABASE_URL is required and has no default.\n"
            "  Set it in the process environment, or add it to "
            f"{ENV_FILE} (see .env.example). Must point at dhaka_kacchi_rag "
            "as the read-only rag_reader role."
        )

    # Validate with the maintenance-database check on, same reasoning as
    # the writer loader - reader connections belong in dhaka_kacchi_rag
    # only.
    url = _validate_postgres_url(raw, var_name="RAG_READER_DATABASE_URL", allow_maintenance=False)

    # Normalise the driver and return the wrapped, ready-to-use settings
    # object.
    return RagReaderSettings(sqlalchemy_url=url.set(drivername=_DRIVER))
