"""Idempotent DDL helpers for the Dhaka Kacchi warehouse migrations.

ONE RULE governs this module. Every helper must emit DDL that

  1. succeeds whether or not the object already exists, AND
  2. renders identically in offline mode (`alembic upgrade base:head --sql`),
     where there is no live connection to introspect.

Rule 2 is why nothing here calls ``sa.inspect(op.get_bind())``: in offline mode
there is nothing to inspect. It is also why we lean on Postgres' own
``IF [NOT] EXISTS`` grammar, which Alembic renders natively via the
``if_not_exists`` / ``if_exists`` parameters.

HONESTY NOTE: ``IF NOT EXISTS`` is *existence*-idempotent, not
*definition*-convergent. A table that already exists with the WRONG SHAPE is
skipped silently by Postgres, with only a NOTICE. No guard in Alembic or
Postgres fixes that. ``warehouse/verify.py`` is the compensating control - run
``make verify`` after every upgrade.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.dialects.postgresql import insert as pg_insert

# ---------------------------------------------------------------------------
# Shared column vocabulary (see ARCHITECTURE.md section 5 and the plan):
#   UUID PKs defaulted by gen_random_uuid() - native since PG13, no pgcrypto
#   all timestamps timestamptz, never bare timestamp
#   money: numeric(12,2) order-level, numeric(14,4) per-unit
#   text unconditionally, never varchar(n)
#   text + CHECK, never native Postgres ENUM types
# ---------------------------------------------------------------------------

UUID = pg.UUID(as_uuid=True)
TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)
JSONB = pg.JSONB(astext_type=sa.Text())
TEXT = sa.Text()
TEXT_ARRAY = pg.ARRAY(sa.Text())
MONEY = sa.Numeric(12, 2)  # order-level amounts
UNIT_MONEY = sa.Numeric(14, 4)  # per-unit prices / derived costs
RATIO = sa.Numeric(3, 2)  # 0.00-1.00 confidence / score
QTY = sa.Numeric(14, 4)  # recipe quantity, in the ingredient's own unit
YIELD = sa.Numeric(6, 4)  # usable fraction after trim/cook loss, (0,1]
LINE_QTY = sa.Numeric(10, 3)  # order-line quantity; numeric, not int (catering half-portions)
RATING = sa.Numeric(2, 1)  # review rating, normalised to 1.0-5.0 at ingest

NOW = sa.text("now()")
NEW_UUID = sa.text("gen_random_uuid()")


# Column FACTORIES, not module-level Column constants: a sa.Column instance
# binds itself to the first Table that consumes it, and reusing one across two
# create_table calls raises "Column object is already assigned to Table".


def pk_column(name: str = "id") -> sa.Column:
    """UUID primary key, server-defaulted so INSERTs need not supply it."""
    return sa.Column(name, UUID, primary_key=True, nullable=False, server_default=NEW_UUID)


def pk_constraint(table: str, *columns: str) -> sa.PrimaryKeyConstraint:
    """Explicitly named PRIMARY KEY.

    Without this, Postgres falls back to its own `<table>_pkey`, which is the
    only constraint in the schema not carrying a house prefix. verify.py's
    naming check treats that inconsistency as a failure rather than tolerating
    a special case.
    """
    return sa.PrimaryKeyConstraint(*(columns or ("id",)), name=pk(table))


def slug_column(name: str = "slug") -> sa.Column:
    """Human-readable natural key on reference tables. Pair with an inline UNIQUE."""
    return sa.Column(name, TEXT, nullable=False)


def created_at_column(name: str = "created_at") -> sa.Column:
    return sa.Column(name, TIMESTAMPTZ, nullable=False, server_default=NOW)


def updated_at_column(name: str = "updated_at") -> sa.Column:
    return sa.Column(name, TIMESTAMPTZ, nullable=False, server_default=NOW)


# ---------------------------------------------------------------------------
# Identifier naming. House convention: idx_<table>_<column>.
# Postgres NAMEDATALEN is 64, so identifiers are silently truncated at 63
# characters - which can quietly collapse two distinct names into one.
# _fit() makes that failure loud-by-construction instead.
# ---------------------------------------------------------------------------

_MAX_IDENT = 63


def _fit(name: str) -> str:
    if len(name) <= _MAX_IDENT:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[: _MAX_IDENT - 9]}_{digest}"


def idx(table: str, *columns: str, suffix: str | None = None) -> str:
    """idx_<table>_<col>[_<col>...][_<suffix>] - suffix labels partial indexes."""
    parts = ["idx", table, *columns] + ([suffix] if suffix else [])
    return _fit("_".join(parts))


def uq(table: str, *columns: str) -> str:
    return _fit("_".join(["uq", table, *columns]))


def ck(table: str, label: str) -> str:
    return _fit(f"ck_{table}_{label}")


def fk(table: str, *columns: str) -> str:
    return _fit("_".join(["fk", table, *columns]))


def pk(table: str) -> str:
    return _fit(f"pk_{table}")


def excl(table: str, label: str) -> str:
    return _fit(f"excl_{table}_{label}")


def _q(identifier: str) -> str:
    """Quote a Postgres identifier for interpolation into raw DDL."""
    return '"' + identifier.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def create_table_if_absent(table_name: str, *columns: Any, **kw: Any) -> sa.Table:
    """CREATE TABLE IF NOT EXISTS <table_name> (...).

    Declare PRIMARY KEY, FOREIGN KEY, UNIQUE and CHECK constraints *inside* this
    call. They are then rendered inside the CREATE TABLE statement and inherit
    its IF NOT EXISTS guard for free. That matters a lot, because Postgres has
    no ``ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS`` - a constraint added by
    a follow-up ALTER has no native guard at all (see replace_constraint).

    Indexes are NOT created here: Alembic's create_table does not emit attached
    sa.Index objects, and Column(index=True) is silently dropped. Every index
    needs its own create_index_if_absent() call.
    """
    return op.create_table(table_name, *columns, if_not_exists=True, **kw)


def drop_table_if_present(table_name: str, **kw: Any) -> None:
    """DROP TABLE IF EXISTS. Takes the table's own indexes and constraints with
    it, which is why most downgrade() bodies are a single line.

    Deliberately NOT CASCADE: downgrades run in reverse chain order, so children
    (order_line) are dropped before parents (orders) anyway. A stray dependency
    should fail loudly rather than take unknown objects with it.
    """
    op.drop_table(table_name, if_exists=True, **kw)


# ---------------------------------------------------------------------------
# Indexes - including partial and partial-unique
# ---------------------------------------------------------------------------


def create_index_if_absent(
    table_name: str,
    columns: Sequence[str],
    *,
    name: str | None = None,
    unique: bool = False,
    where: str | sa.sql.ClauseElement | None = None,
    using: str | None = None,
    suffix: str | None = None,
    **kw: Any,
) -> str:
    """CREATE [UNIQUE] INDEX IF NOT EXISTS <name> ON <table> (<cols>) [WHERE ...].

    Partial UNIQUE rules must come through here rather than being declared as a
    UniqueConstraint: Postgres cannot express partial uniqueness as a table
    constraint, only as an index.

    Returns the resolved index name so downgrade() can reuse it.
    """
    resolved = name or idx(table_name, *columns, suffix=suffix)
    if where is not None:
        kw["postgresql_where"] = sa.text(where) if isinstance(where, str) else where
    if using is not None:
        kw["postgresql_using"] = using
    op.create_index(resolved, table_name, list(columns), unique=unique, if_not_exists=True, **kw)
    return resolved


def drop_index_if_present(name: str, table_name: str | None = None, **kw: Any) -> None:
    op.drop_index(name, table_name=table_name, if_exists=True, **kw)


# ---------------------------------------------------------------------------
# Constraints added AFTER the table exists (avoid if you possibly can)
# ---------------------------------------------------------------------------


def replace_constraint(table_name: str, constraint_name: str, definition: str) -> None:
    """Idempotently (re)define a constraint that cannot be declared inline.

    Postgres has DROP CONSTRAINT IF EXISTS but no ADD CONSTRAINT IF NOT EXISTS,
    so drop-then-add is the only guard available. It is strictly better than a
    bare existence check: it also CONVERGES. An out-of-band constraint carrying
    the right name and the wrong definition gets repaired rather than skipped.

    Both statements run inside the migration's transaction (Postgres has
    transactional DDL), so no other session ever observes the table
    unconstrained, and a failure on ADD rolls the DROP back.

    Prefer inline declaration in create_table_if_absent(). Reach for this only
    when a constraint genuinely cannot be inline - e.g. an EXCLUDE constraint,
    which SQLAlchemy's CreateTable does not render inline.

    GOTCHA: op.execute() wraps a str in sa.text(), which treats ``:word`` as a
    bind parameter. ``::type`` casts are safe (text()'s negative lookbehind on
    ':'), but a bare colon inside a string literal is not. Keep colons out of
    constraint bodies - trivial, since the house style is
    ``CHECK (severity IN ('info','warn','critical'))``.

    LARGE-TABLE CAVEAT: ADD CONSTRAINT takes ACCESS EXCLUSIVE and validates every
    existing row. Once a table has real volume, add it NOT VALID here and run
    VALIDATE CONSTRAINT in a separate, non-transactional step.
    """
    t, c = _q(table_name), _q(constraint_name)
    op.execute(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS {c}")
    op.execute(f"ALTER TABLE {t} ADD CONSTRAINT {c} {definition}")


def drop_constraint_if_present(
    constraint_name: str, table_name: str, type_: str | None = None
) -> None:
    op.drop_constraint(constraint_name, table_name, type_=type_, if_exists=True)


# ---------------------------------------------------------------------------
# Columns and extensions
# ---------------------------------------------------------------------------


def add_column_if_absent(table_name: str, column: sa.Column, **kw: Any) -> None:
    op.add_column(table_name, column, if_not_exists=True, **kw)


def drop_column_if_present(table_name: str, column_name: str, **kw: Any) -> None:
    op.drop_column(table_name, column_name, if_exists=True, **kw)


def create_extension_if_absent(name: str) -> None:
    op.execute(f"CREATE EXTENSION IF NOT EXISTS {_q(name)}")


def drop_extension_if_present(name: str) -> None:
    op.execute(f"DROP EXTENSION IF EXISTS {_q(name)}")


# ---------------------------------------------------------------------------
# Seed DML (see 0012_seed.py)
# ---------------------------------------------------------------------------


def upsert(
    table: sa.TableClause,
    rows: Sequence[dict[str, Any]],
    *,
    conflict_on: Sequence[str],
    update: Sequence[str] | None = None,
) -> None:
    """Idempotent INSERT for seed data.

    update=None  -> ON CONFLICT DO NOTHING. Use for rows representing business
                    events (orders, order_line, review, ad_spend): re-running
                    the seed must never rewrite something an operator or an
                    ingest job has since touched.

    update=[...] -> ON CONFLICT DO UPDATE. Use for reference rows keyed by their
                    natural slug (menu_item, ingredient, supplier): the seed file
                    is the declared truth, so a re-run should CONVERGE the row to
                    it rather than skip it. This is what makes editing a seed
                    price and re-running actually do something.

    Columns an agent or operator owns (reliability_score, name_de/name_bn, a
    current_price replaced by a real invoice) are deliberately left OUT of the
    caller's `update` list so a redeploy cannot reset them.

    `table` is a lightweight sa.table()/sa.column() construct, NOT an ORM model.
    Migrations must never import application models: revision 0012 has to keep
    working after the real schema has moved on.
    """
    if not rows:
        return
    stmt = pg_insert(table).values(list(rows))
    if update:
        cols = list(update)
        stmt = stmt.on_conflict_do_update(
            index_elements=list(conflict_on),
            set_={c: stmt.excluded[c] for c in cols},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_on))
    op.execute(stmt)


def delete_by(table: sa.TableClause, column: str, values: Iterable[Any]) -> None:
    """Reverse of upsert(): remove exactly the rows this migration inserted."""
    vals = list(values)
    if not vals:
        return
    op.execute(sa.delete(table).where(table.c[column].in_(vals)))
