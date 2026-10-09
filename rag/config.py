"""The single accessor for RAG-subsystem configuration.

House rule, mirrored from warehouse/config.py: nothing outside this module
reads os.environ for anything RAG-related. Everything takes a settings
object. Validation is fail-fast at load time, not at first query.

Four loaders, not one combined Settings object (see rag/RAG_progress.md
decision #8): each consumer gets exactly the credential it needs and no others.

  load_rag_admin_settings()           - one-time setup only (CREATE DATABASE,
                                        CREATE EXTENSION vector, role
                                        creation). Never imported by ingestion
                                        or agent code.
  load_rag_writer_settings()          - the recurring chunk-ingestion job.
                                        Writes every store table, reads none
                                        for its own purposes.
  load_rag_public_reader_settings()   - a caller that may read PUBLIC stores
                                        only. This is what a customer-facing
                                        process runs with.
  load_rag_internal_reader_settings() - a caller that may read EVERY store:
                                        the business's own agents and tools.

The last two share one dataclass (the difference between them is enforced by
Postgres grants, not by the shape of a Python object - see
_load_reader_settings below), but each keeps its own env var and its own
loader, so a process can only ever load the one credential it is meant to
hold.

This module also owns the STORE REGISTRY: the list of vector stores the
subsystem knows about, read from rag/stores.toml by load_store_registry().
The registry is configuration (which stores exist, what each is called, how
big its chunks are), NOT enforcement (who may read what) - enforcement lives
in Postgres grants. See rag/MULTI_STORE_DESIGN.md sections 5 and 9.3.
"""

# This makes type hints like `URL | None` work even on older Python
# versions that don't natively support that syntax - it tells Python to
# treat all type annotations as text instead of evaluating them right away.
from __future__ import annotations

# The standard library module for reading environment variables
# (os.environ) - this is the ONLY file in rag/ allowed to touch it, per
# the house rule stated in the docstring above.
import os

# `re` is the standard library's regular-expression module. Used here to
# check that a physical table name from stores.toml is a plain, safe SQL
# identifier before it is ever allowed near a query (see
# _IDENTIFIER_PATTERN further down).
import re

# `tomllib` reads TOML files. It has been part of the standard library since
# Python 3.11, so reading the store registry as TOML costs us no extra
# dependency at all - which is exactly why that format was chosen.
import tomllib

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

# The store registry: the list of vector stores, each with its logical name,
# physical table, visibility tier and chunk settings. Lives next to this file
# (inside rag/) rather than at the repo root, because it describes this
# subsystem specifically and nothing outside rag/ has any business reading it.
STORES_FILE = Path(__file__).resolve().parent / "stores.toml"

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


def _load_reader_settings(
    environ: Mapping[str, str] | None,
    *,
    var_name: str,
    role_description: str,
) -> RagReaderSettings:
    """The shared body of all three reader loaders below.

    There is one reader settings DATACLASS but three reader ROLES, and that
    asymmetry is deliberate. The three roles differ in what the database will
    let them read (public stores only vs every store), but that difference is
    enforced by Postgres grants - not by anything the caller's Python objects
    are shaped like. Giving each role its own dataclass would suggest the
    type system was providing a guarantee it is not: `retrieve()` accepts any
    reader settings, and the database decides what it can actually reach.

    What genuinely must not blur together is WHICH credential a process
    loads, so each role gets its own env var and its own loader below - the
    least-privilege rule from RAG_progress.md decision #8, unchanged.
    """
    # Same environment-resolution step as every other loader.
    environ = _load_environ(environ)

    # Read and clean up the reader-specific connection string.
    raw = (environ.get(var_name) or "").strip()

    # Same fail-fast pattern once more: no silent defaults, ever.
    if not raw:
        raise ConfigError(
            f"{var_name} is required and has no default.\n"
            "  Set it in the process environment, or add it to "
            f"{ENV_FILE} (see .env.example). Must point at dhaka_kacchi_rag "
            f"as the {role_description}."
        )

    # Validate with the maintenance-database check on, same reasoning as the
    # writer loader - reader connections belong in dhaka_kacchi_rag only.
    url = _validate_postgres_url(raw, var_name=var_name, allow_maintenance=False)

    # Normalise the driver and return the wrapped, ready-to-use settings
    # object.
    return RagReaderSettings(sqlalchemy_url=url.set(drivername=_DRIVER))


def load_rag_public_reader_settings(
    environ: Mapping[str, str] | None = None,
) -> RagReaderSettings:
    """Fail-fast config for a caller that may read PUBLIC stores only.

    This is the credential a customer-facing process runs with. It is the
    whole reason the multi-store design exists: whatever an LLM on this
    connection is tricked into asking for, Postgres will refuse to return
    anything from a store marked internal (rag/MULTI_STORE_DESIGN.md
    sections 1 and 6).
    """
    return _load_reader_settings(
        environ,
        var_name="RAG_PUBLIC_READER_DATABASE_URL",
        role_description="rag_public_reader role (public stores only)",
    )


def load_rag_internal_reader_settings(
    environ: Mapping[str, str] | None = None,
) -> RagReaderSettings:
    """Fail-fast config for a caller that may read EVERY store.

    Used by the business's own agents and tooling. It holds this credential
    because of WHO it is, not because of what it was asked - the same
    principle as every other role here.
    """
    return _load_reader_settings(
        environ,
        var_name="RAG_INTERNAL_READER_DATABASE_URL",
        role_description="rag_internal_reader role (all stores)",
    )


# ---------------------------------------------------------------------------
# The store registry
# ---------------------------------------------------------------------------
#
# Everything below describes the STORES this subsystem knows about: the
# logical name a caller uses, the physical table it maps to, the visibility
# tier it belongs to, and the chunk settings its loader should use.
#
# Read from rag/stores.toml (that file's own header explains each field, and
# why). Loading is fail-fast, exactly like the URL loaders above: a malformed
# registry aborts the process at startup rather than surfacing later as a
# confusing error on the first query.

# The only two visibility tiers that exist, and the reason this whole design
# exists: a caller holding the public reader credential must never be able to
# read an internal store. Kept as one tuple so the validation check below and
# the error message it produces can never drift apart from each other.
_VISIBILITIES = ("public", "internal")

# A physical table name must match this, and nothing else.
#
# This check matters more than it first looks. Postgres has no syntax for "a
# parameter standing in for a table name", so a table name is always spliced
# directly into query text - it can never be a bind parameter. Callers never
# supply a table name (they pass a logical name, which is only ever used as a
# dictionary key), so this is not the last line of defence against an
# attacker; it is the line that stops a typo or a careless edit in
# stores.toml from turning into a SQL-injection vector. Read the pattern as:
# "starts with a lowercase letter or an underscore, then contains only
# lowercase letters, digits and underscores, all the way to the end".
_IDENTIFIER_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")

# Every field a [stores.<name>] block must define. Listed once, here, so the
# validator below can loop over it instead of repeating five near-identical
# checks - and so making a field required later is a one-line change.
#
# `source_dir` is deliberately NOT in this list: a store can legitimately have
# no folder at all (one fed from a database export, or populated by hand).
# Only the file loader insists on it, and it says so itself with a clear error.
_REQUIRED_STORE_FIELDS = (
    "table",
    "visibility",
    "chunk_size_tokens",
    "overlap_tokens",
    "description",
)

# The folder every store's `source_dir` is relative to. A store's files live
# under rag/Knowledge_Base/<source_dir>/, which is also what makes a chunk's
# `source_path` (e.g. "public/brand-book.md") mean the same thing to the
# loader, the retriever, the web UI's file-serving endpoint and a human
# reading the database. Resolved once, absolutely, here rather than being
# recomputed in each loader.
KNOWLEDGE_BASE_DIR = (Path(__file__).resolve().parent / "Knowledge_Base").resolve()

# What a `source_dir` value is allowed to look like.
#
# Same reasoning as _IDENTIFIER_PATTERN above, one layer down: this string
# becomes a real filesystem path, so it must not be able to point anywhere
# except a folder under Knowledge_Base/. The pattern allows letters, digits
# and the three punctuation characters a folder name might reasonably use -
# but NOT a leading slash (an absolute path), NOT "..", and NOT a Windows
# drive letter or backslash. Read it as: "one or more path segments, each made
# of letters, digits, dots, dashes and underscores, joined by forward slashes".
_SOURCE_DIR_PATTERN = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


class UnknownStoreError(LookupError):
    """A caller named a store that it cannot search.

    Deliberately raised for TWO different situations (design doc section 7.2):

      1. the name is not in the registry at all, and
      2. the name is in the registry, but this caller's credential has no
         SELECT grant on its table.

    Both must look IDENTICAL from the outside. If "you may not read that"
    could be told apart from "no such store", a public caller could learn
    that an internal store exists simply by asking for it - a real
    information leak even though no rows are ever returned. Using one
    exception type for both cases makes that guarantee structural instead of
    something a future edit has to remember.
    """


@dataclass(frozen=True, slots=True)
class Store:
    """Everything known about one store, straight from the registry.

    A plain, inert data holder on purpose - it deliberately offers no
    `is_public` helper or anything similar. The moment code starts branching
    on a store's declared visibility, the config file has quietly become an
    enforcement layer, which is exactly what design doc section 9.3 forbids.
    Access decisions belong to Postgres grants; this object only describes.
    """

    # The logical name a caller passes, e.g. "social_share" (this is also the
    # key the store is filed under in the registry, kept here as well so a
    # Store carries its own identity wherever it is passed around).
    name: str
    # The physical table in dhaka_kacchi_rag holding this store's chunks.
    # Read only from this config file - never from a caller.
    table: str
    # "public" or "internal". A declaration of intent, checked against the
    # real grants by verify_stores.py, never trusted for access control.
    visibility: str
    # WHICH FOLDER under rag/Knowledge_Base/ this store's content comes from,
    # e.g. "public" or "internal" - or None for a store that is not fed from a
    # folder (one loaded from a database export, or populated by hand).
    #
    # The folder is what decides a file's store, and therefore its visibility:
    # dropping a file into Knowledge_Base/internal/ makes it internal, and no
    # config edit is involved. That is the whole reason it is a folder rather
    # than a list of filenames - visibility-by-placement is auditable at a
    # glance, in the filesystem, by a human who has never read this code.
    source_dir: str | None
    # How big each chunk should be, measured in the embedding model's own
    # tokens. Read only by the loaders; retrieval never looks at it.
    chunk_size_tokens: int
    # How many tokens consecutive chunks share at their boundary. Must be
    # strictly smaller than chunk_size_tokens, or the chunker's sliding
    # window would never move forward through the text.
    overlap_tokens: int
    # Plain-language summary of what this store holds. This is PROMPT TEXT:
    # an AI agent reads it when choosing which store to search.
    description: str


class StoreRegistry:
    """The loaded set of stores, looked up by logical name.

    Deliberately small. A caller's store name is only ever used as a lookup
    key against this object - which is the single reason a caller-supplied
    string can never reach SQL text (design doc section 9.1, defence 1).
    """

    def __init__(self, stores: Mapping[str, Store]) -> None:
        # Copy into a plain dict, so whoever passed the mapping in cannot
        # later mutate the registry out from under a running process.
        self._stores: dict[str, Store] = dict(stores)

    def __contains__(self, name: object) -> bool:
        # Lets callers write `if "social_share" in registry` directly,
        # without reaching for the internal dictionary.
        return name in self._stores

    def __len__(self) -> int:
        # How many stores are registered - used by the verifier's summary
        # output and by tests.
        return len(self._stores)

    def __iter__(self):
        # Iterating a registry yields its Store objects, not its names, so
        # `for store in registry` reads naturally at every call site (and is
        # how list_stores() walks every candidate store).
        return iter(self._stores.values())

    def get(self, name: str) -> Store:
        """Return the Store called `name`, or raise UnknownStoreError.

        The error message is deliberately GENERIC and names no other store.
        Listing the available stores here would be helpful for a developer
        and dangerous for everyone else: a public caller asking for
        "hr_policies" would be told, in the error text, exactly which stores
        it is not allowed to know about. Uniform and uninformative is the
        correct behaviour for anything caller-supplied.
        """
        try:
            # The one and only use of a caller-supplied store name: a
            # dictionary lookup. Nothing else is ever done with it.
            return self._stores[name]
        except KeyError:
            # `from None` suppresses the original KeyError as "cause", so the
            # caller sees one clean message rather than a chain of two.
            raise UnknownStoreError(f"unknown store {name!r}") from None


def load_store_registry(path: Path | None = None) -> StoreRegistry:
    """Read the store registry from `path`, defaulting to rag/stores.toml.

    Validates every entry and raises ConfigError on the first problem found,
    with a message naming the offending store and field - a bad registry is a
    deployment mistake that should stop the process at startup, not produce
    strange behaviour on the first query.

    The optional `path` exists so tests (and verify_stores.py) can point at a
    different file without touching the real registry.
    """
    # Fall back to the real file only when no explicit path was given.
    stores_path = path if path is not None else STORES_FILE

    # tomllib insists on BINARY mode ("rb") because TOML is defined as UTF-8
    # and the library wants to do the decoding itself.
    try:
        with stores_path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        # A missing registry is almost always a wrong working directory or a
        # file that never got created - say so plainly instead of letting a
        # bare traceback appear.
        raise ConfigError(
            f"store registry not found at {stores_path}.\n"
            "  The RAG subsystem cannot start without it - see rag/stores.toml."
        ) from None
    except tomllib.TOMLDecodeError as exc:
        # A syntax error in the file itself (a stray quote, a missing
        # bracket). tomllib's own message says where, so pass it through.
        raise ConfigError(f"{stores_path} is not valid TOML: {exc}") from None

    # The file is expected to contain exactly one top-level table named
    # [stores], whose keys are the logical store names.
    raw_stores = data.get("stores")

    # Reject a file with no stores at all: an empty registry would make every
    # search fail with "unknown store", which looks like a code bug rather
    # than the configuration mistake it actually is.
    if not isinstance(raw_stores, dict) or not raw_stores:
        raise ConfigError(
            f"{stores_path} must define at least one [stores.<name>] block."
        )

    # The validated result, keyed by logical store name.
    stores: dict[str, Store] = {}

    # Which physical table each declared store claims, so that a second store
    # cannot silently claim the same one. Two logical names pointing at one
    # table would give the same rows two different visibility tiers - a
    # contradiction the rest of this design has no way to resolve, and one
    # that would show up as a confusing verifier failure rather than a clear
    # configuration error.
    claimed_tables: dict[str, str] = {}

    # Walk each declared store, in the order the file lists them.
    for name, block in raw_stores.items():
        # Each [stores.<name>] block must be a table of key = value pairs.
        if not isinstance(block, dict):
            raise ConfigError(f"{stores_path}: [stores.{name}] must be a table.")

        # Report a missing field BY NAME, rather than failing later with a
        # bare KeyError that says nothing about which store was at fault.
        missing = [field for field in _REQUIRED_STORE_FIELDS if field not in block]
        if missing:
            raise ConfigError(
                f"{stores_path}: [stores.{name}] is missing "
                f"{', '.join(missing)}."
            )

        # Pull each field out once, into a local, so the checks below read as
        # prose rather than as repeated dictionary lookups.
        table = block["table"]
        visibility = block["visibility"]
        chunk_size_tokens = block["chunk_size_tokens"]
        overlap_tokens = block["overlap_tokens"]
        description = block["description"]

        # source_dir is OPTIONAL - absent means "this store is not fed from a
        # folder". `.get(None)` rather than `[...]` so a block without it is
        # valid rather than a KeyError.
        source_dir = block.get("source_dir")

        # When it IS given, it has to be a safe relative path: this value
        # becomes a real directory the loader walks (and, in webui.py, the
        # prefix that decides which files may be served), so an absolute path
        # or a ".." segment would let the config point the loader at
        # arbitrary parts of the filesystem. Same spirit as the table-name
        # check below, applied to a path instead of an SQL identifier.
        if source_dir is not None and (
            not isinstance(source_dir, str) or not _SOURCE_DIR_PATTERN.match(source_dir)
        ):
            raise ConfigError(
                f"{stores_path}: [stores.{name}] source_dir {source_dir!r} must be "
                "a relative path under Knowledge_Base/ using letters, digits, "
                "'.', '-' and '_' separated by '/' (no leading '/', no '..')."
            )

        # The table name will eventually be spliced into SQL text, so it has
        # to be a plain identifier - see _IDENTIFIER_PATTERN's comment above
        # for why this check exists at all.
        if not isinstance(table, str) or not _IDENTIFIER_PATTERN.match(table):
            raise ConfigError(
                f"{stores_path}: [stores.{name}] table {table!r} is not a safe "
                "SQL identifier (lowercase letters, digits and underscores only)."
            )

        # No two stores may share a physical table - see claimed_tables above.
        if table in claimed_tables:
            raise ConfigError(
                f"{stores_path}: [stores.{name}] and "
                f"[stores.{claimed_tables[table]}] both claim table {table!r}. "
                "Each table may back only one store."
            )
        # Remember this claim so a later store is checked against it.
        claimed_tables[table] = name

        # The visibility tier must be one of the two known values. This is
        # also what stops a typo like "Public" or "internal " from silently
        # creating a third tier that no grant and no verifier knows about.
        if visibility not in _VISIBILITIES:
            raise ConfigError(
                f"{stores_path}: [stores.{name}] visibility {visibility!r} must "
                f"be one of {', '.join(_VISIBILITIES)}."
            )

        # Chunk size must be a positive whole number of tokens; a zero or
        # negative size would make the chunker produce nonsense.
        if not isinstance(chunk_size_tokens, int) or chunk_size_tokens <= 0:
            raise ConfigError(
                f"{stores_path}: [stores.{name}] chunk_size_tokens must be a "
                f"positive integer, got {chunk_size_tokens!r}."
            )

        # Overlap may be zero (no overlap at all is legal) but never negative.
        if not isinstance(overlap_tokens, int) or overlap_tokens < 0:
            raise ConfigError(
                f"{stores_path}: [stores.{name}] overlap_tokens must be zero or "
                f"a positive integer, got {overlap_tokens!r}."
            )

        # The same rule chunking.py enforces at runtime, caught here instead
        # so the mistake is reported at startup against the file that caused
        # it: with overlap >= size the sliding window never advances, and the
        # chunker would raise on every single document.
        if overlap_tokens >= chunk_size_tokens:
            raise ConfigError(
                f"{stores_path}: [stores.{name}] overlap_tokens "
                f"({overlap_tokens}) must be smaller than chunk_size_tokens "
                f"({chunk_size_tokens})."
            )

        # The description is read by a model choosing where to search, so an
        # empty one is a real defect, not a cosmetic one.
        if not isinstance(description, str) or not description.strip():
            raise ConfigError(
                f"{stores_path}: [stores.{name}] description must be a "
                "non-empty string - an agent reads it to choose a store."
            )

        # Everything checked out: build the immutable record for this store.
        stores[name] = Store(
            name=name,
            table=table,
            visibility=visibility,
            source_dir=source_dir,
            chunk_size_tokens=chunk_size_tokens,
            overlap_tokens=overlap_tokens,
            description=description,
        )

    # Hand back the registry, ready to be looked up by logical name.
    return StoreRegistry(stores)
