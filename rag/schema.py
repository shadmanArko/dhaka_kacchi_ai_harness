"""Create the `chunks` table inside dhaka_kacchi_rag, and grant rag_writer
/ rag_reader exactly the privileges each one needs on it.

Run this AFTER rag/bootstrap_db.py - it depends on the database, the
`vector` extension, and both roles already existing. See RAG_progress.md
decision #10 for the column-by-column reasoning behind this schema, and
decision #11 for why this is a plain script rather than an Alembic
migration.

Safe to run any number of times - every statement is written to be a
no-op when it's already been applied.
"""

# Same "evaluate type hints lazily" import as every other file in rag/.
from __future__ import annotations

import sys

# `sa` for raw SQL execution, same as bootstrap_db.py.
import sqlalchemy as sa

# We only need the admin settings here too - creating a table and granting
# privileges both require elevated rights, not the restricted rights
# rag_writer/rag_reader themselves have.
from rag.bootstrap_db import RAG_DATABASE_NAME, RAG_READER_ROLE, RAG_WRITER_ROLE
from rag.config import ConfigError, RagAdminSettings, load_rag_admin_settings

# The fixed list of source formats this table currently accepts, matching
# what was decided in RAG_progress.md decision #10. Written once here as a
# Python tuple so the CHECK constraint's SQL text and this comment can't
# drift apart from each other by accident.
# "html" added 2026-10-02 (decision #26) - real SEC EDGAR exhibit
# documents turned up in rag/Knowledge_Base/, and the original decision
# #10 list was all file formats that existed as of that decision, not an
# exhaustive final list - exactly what text+CHECK (over a native ENUM) is
# FOR: widening this later without a breaking migration.
SOURCE_TYPES = ("pdf", "markdown", "docx", "txt", "csv", "xlsx", "html")


def _admin_engine_on_rag_db(settings: RagAdminSettings) -> sa.Engine:
    # settings.sqlalchemy_url points at the `postgres` maintenance database
    # (see RagAdminSettings' own docstring in config.py) - but creating a
    # table has to happen INSIDE dhaka_kacchi_rag itself, not in
    # `postgres`. So, same trick as bootstrap_db.py's create_extension:
    # take the admin URL and swap just the database name portion.
    target_url = settings.sqlalchemy_url.set(database=RAG_DATABASE_NAME)
    # No AUTOCOMMIT here, unlike bootstrap_db.py - CREATE TABLE and GRANT
    # are both allowed to run inside a normal transaction, so we let
    # SQLAlchemy's default transactional behaviour apply (the whole thing
    # either fully commits or fully rolls back together).
    return sa.create_engine(target_url, poolclass=sa.pool.NullPool)


def create_chunks_table(settings: RagAdminSettings) -> int:
    """CREATE TABLE chunks, with every column and constraint decided in
    RAG_progress.md decision #10.
    """
    # Build the comma-separated SQL fragment 'pdf','markdown','docx',...
    # from the Python tuple above, so the CHECK constraint's allowed
    # values always match SOURCE_TYPES exactly.
    source_type_values = ", ".join(f"'{value}'" for value in SOURCE_TYPES)

    with _admin_engine_on_rag_db(settings).begin() as conn:
        # `.begin()` (rather than `.connect()`) opens an explicit
        # transaction that auto-commits on success and auto-rolls-back on
        # any exception - appropriate here since we want the table AND its
        # grants (see grant_privileges below, called separately) to each
        # be all-or-nothing.
        conn.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS chunks (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    -- source_type: which kind of source document this
                    -- chunk was cut from - text+CHECK, never a native
                    -- Postgres ENUM, matching this repo's own convention
                    -- (an ENUM value can never be dropped once used; a
                    -- CHECK's allowed list can be widened with a plain
                    -- ALTER TABLE later).
                    source_type text NOT NULL,
                    -- source_path: where the original document lives, so
                    -- a human or agent can go look at the real source if
                    -- a chunk's content is ever wrong or needs updating.
                    source_path text NOT NULL,
                    -- chunk_index: this chunk's position within that
                    -- source document (0, 1, 2, ...) - source_path alone
                    -- isn't precise enough for a multi-page/multi-section
                    -- source, since many chunks can come from one file.
                    chunk_index integer NOT NULL,
                    -- chunk_text: the actual retrievable text content of
                    -- this chunk - what gets shown to the agent/LLM once
                    -- this row is retrieved.
                    chunk_text text NOT NULL,
                    -- embedding: the 1024-dimensional vector representation
                    -- of chunk_text, produced by the BAAI/bge-m3 model
                    -- (RAG_progress.md decision #17, switched from the
                    -- 384-dim all-MiniLM-L6-v2 after it badly mangled real
                    -- Bengali captions) - this is what similarity search
                    -- actually compares against.
                    embedding vector(1024) NOT NULL,
                    -- metadata: anything extra about this chunk that
                    -- varies by source type and doesn't deserve its own
                    -- fixed column - e.g. a PDF page number, a markdown
                    -- heading, a CSV row range. Stored as jsonb (flexible
                    -- shape), matching the same pattern already used by
                    -- customer.contact_hashes / ingredient.price_history /
                    -- review.aspects elsewhere in this codebase.
                    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                    -- created_at: when this row was first inserted - set
                    -- once, never touched again after that.
                    created_at timestamptz NOT NULL DEFAULT now(),
                    -- updated_at: when this row was last changed (initial
                    -- insert OR a later upsert) - the ingestion job is
                    -- responsible for setting this to now() on every
                    -- upsert, not just at first insert.
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    -- UNIQUE (source_path, chunk_index): the constraint
                    -- that makes re-running ingestion on an unchanged
                    -- source an idempotent upsert instead of a pile of
                    -- duplicate rows - same shape as orders' own
                    -- UNIQUE (channel, external_id).
                    CONSTRAINT uq_chunks_source_path_chunk_index
                        UNIQUE (source_path, chunk_index)
                )
                """
            )
        )

        # The CHECK constraint is applied as a SEPARATE step, always run,
        # rather than inline in CREATE TABLE - CREATE TABLE IF NOT EXISTS
        # is existence-idempotent, not definition-convergent (same lesson
        # already documented in this repo's own CLAUDE.md "Alembic traps"
        # section): on an ALREADY-EXISTING table, changing SOURCE_TYPES
        # above and re-running this script would otherwise silently do
        # nothing, leaving the live constraint stale. Drop-then-add
        # converges it to match SOURCE_TYPES on every run, exactly the
        # same "replace_constraint()" idea already used elsewhere in this
        # codebase for the same reason.
        conn.execute(sa.text("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS ck_chunks_source_type"))
        conn.execute(
            sa.text(
                f"ALTER TABLE chunks ADD CONSTRAINT ck_chunks_source_type "
                f"CHECK (source_type IN ({source_type_values}))"
            )
        )
    print("ensured table 'chunks' exists in dhaka_kacchi_rag")
    return 0


def grant_privileges(settings: RagAdminSettings) -> int:
    """GRANT rag_writer and rag_reader exactly the privileges each needs
    on the chunks table - nothing more. This is the step that actually
    enforces the "agents can only read, never write" rule from
    RAG_progress.md decision #4/#7; role creation alone (bootstrap_db.py)
    doesn't grant any table access by itself.
    """
    with _admin_engine_on_rag_db(settings).begin() as conn:
        # A role needs CONNECT on the database before anything else it's
        # granted can matter - stated explicitly here rather than relying
        # on Postgres' own default (new roles can usually already connect
        # to any database by default, but writing this out makes the
        # intent visible in the SQL itself instead of depending on a
        # server-wide default that could be different on another machine).
        conn.execute(
            sa.text(f"GRANT CONNECT ON DATABASE {RAG_DATABASE_NAME} TO {RAG_WRITER_ROLE}")
        )
        conn.execute(
            sa.text(f"GRANT CONNECT ON DATABASE {RAG_DATABASE_NAME} TO {RAG_READER_ROLE}")
        )
        # rag_writer: the recurring ingestion job needs to add new chunks
        # and update existing ones (the upsert-on-re-ingest behaviour) -
        # SELECT is included too, because an upsert has to be able to
        # check "does this row already exist" before deciding whether to
        # INSERT or UPDATE it. DELETE added 2026-10-02 (RAG_progress.md
        # decision #25) specifically for the re-index "prune orphaned
        # chunks" step - a deliberate, confirmed expansion of this role's
        # privileges, not an oversight. Still no DDL rights of any kind,
        # and still scoped to this one table only.
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON chunks TO {RAG_WRITER_ROLE}"))
        # rag_reader: agents only ever read at query time - SELECT and
        # nothing else, structurally incapable of changing any row.
        conn.execute(sa.text(f"GRANT SELECT ON chunks TO {RAG_READER_ROLE}"))
    print(f"granted chunks privileges to {RAG_WRITER_ROLE!r} and {RAG_READER_ROLE!r}")
    return 0


def main() -> int:
    try:
        # Same fail-fast config loading as bootstrap_db.py - if the admin
        # connection details are missing or invalid, stop here with a
        # clear message.
        settings = load_rag_admin_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # Create the table first, then grant privileges on it - grants would
    # fail if the table didn't exist yet, so the order here isn't
    # arbitrary.
    for step in (create_chunks_table, grant_privileges):
        status = step(settings)
        if status != 0:
            return status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
