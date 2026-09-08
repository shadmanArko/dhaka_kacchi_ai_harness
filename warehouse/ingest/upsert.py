"""Idempotent upsert for application code (ingest jobs), NOT migrations.

Same ON CONFLICT shape as warehouse/migrations/helpers.py's upsert(), but
executed against a plain SQLAlchemy Connection instead of alembic.op - there is
no Alembic operations context outside a migration run, so that helper cannot be
imported here. This one exists because the "land raw, then upsert by natural
key" pattern already appears once (in helpers.py) and ARCHITECTURE.md plans more
ingest/<source>.py jobs that will need the same shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
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
    """Upsert `rows` into `table`, returning `returning` columns for every row.

    NOTE: `ON CONFLICT DO NOTHING ... RETURNING` yields NO row for a skipped
    conflict - only inserted rows come back. A caller that needs a row's id on
    every run regardless of conflict (e.g. attaching order_line to an existing
    order) must pass `update=[...]`, not rely on the DO NOTHING default.
    """
    if not rows:
        return []
    stmt = pg_insert(table).values(list(rows))
    stmt = (
        stmt.on_conflict_do_update(
            index_elements=list(conflict_on), set_={c: stmt.excluded[c] for c in update}
        )
        if update
        else stmt.on_conflict_do_nothing(index_elements=list(conflict_on))
    )
    return conn.execute(stmt.returning(*(table.c[c] for c in returning))).all()
