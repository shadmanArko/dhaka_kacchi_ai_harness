"""Idempotent upsert helper for rag/'s own ingestion code.

A deliberate copy of warehouse/ingest/upsert.py's helper, not an import
from it - see RAG_progress.md decision #14 for why: keeps rag/ fully
independent of warehouse/ at the code level, not just the database-
connection level already decided elsewhere in this subsystem. The two
copies are allowed to drift if either subsystem's needs diverge later.
"""

from __future__ import annotations

# `Sequence` is a generic "ordered, read-only collection" type - used here
# instead of `list` specifically so this function accepts anything
# list-like (a tuple, a list, etc.), not just a literal Python list.
from collections.abc import Sequence

# `Any` means "could be any type" - used below because a single row's
# column values can genuinely be anything (a string, a number, a list of
# floats for the embedding column, etc.), and there's no more specific
# type that covers all of them.
from typing import Any

# The conventional SQLAlchemy alias, same as every other file in rag/ that
# talks to Postgres directly.
import sqlalchemy as sa

# Postgres-specific INSERT statement builder - regular SQLAlchemy `insert`
# doesn't know about ON CONFLICT (that's a Postgres-only SQL feature, not
# part of standard SQL), so this dialect-specific version is required to
# build an upsert at all.
from sqlalchemy.dialects.postgresql import insert as pg_insert


def upsert_returning(
    conn: sa.Connection,
    table: sa.TableClause,
    rows: Sequence[dict[str, Any]],
    *,
    conflict_on: Sequence[str],
    update: Sequence[str] | None = None,
    returning: Sequence[str] = ("id",),
) -> list[sa.Row]:
    """Upsert `rows` into `table`, returning `returning` columns for every
    row actually inserted or updated.

    NOTE (same trap as the warehouse/ version this is copied from):
    `ON CONFLICT DO NOTHING ... RETURNING` yields NO row for a skipped
    conflict - only inserted rows come back. Pass `update=[...]` (rag/'s
    ingestion always will, so `updated_at` gets refreshed on every
    re-ingest) if the caller needs a row back even when nothing about it
    actually changed.
    """
    # Nothing to do for an empty batch - and building a SQL statement with
    # zero VALUES rows would be invalid SQL anyway, so this guard avoids
    # that edge case entirely rather than handling it deeper down.
    if not rows:
        return []

    # Build a Postgres-flavoured INSERT statement carrying every row's
    # values at once - one round-trip to the database for the whole batch,
    # not one INSERT per row.
    stmt = pg_insert(table).values(list(rows))

    # Two different behaviours depending on whether the caller wants
    # conflicting rows updated or silently skipped:
    if update:
        # ON CONFLICT (conflict_on) DO UPDATE SET col = EXCLUDED.col, ...
        # `stmt.excluded[c]` refers to "the value this row WOULD have had
        # if the insert had succeeded" - i.e. the new value we tried to
        # insert - which is exactly what we want to overwrite the
        # existing row's column with.
        stmt = stmt.on_conflict_do_update(
            index_elements=list(conflict_on), set_={c: stmt.excluded[c] for c in update}
        )
    else:
        # ON CONFLICT (conflict_on) DO NOTHING - insert only genuinely new
        # rows, leave any existing conflicting row completely untouched.
        stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_on))

    # Execute the statement and ask Postgres to hand back the requested
    # columns (e.g. "id") for every row that was actually written -
    # `.all()` collects every result row into a plain Python list rather
    # than leaving it as a lazy, one-time-iterable cursor.
    return conn.execute(stmt.returning(*(table.c[c] for c in returning))).all()
