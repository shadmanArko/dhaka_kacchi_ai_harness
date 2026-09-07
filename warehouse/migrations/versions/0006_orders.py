"""orders

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (  # noqa: F401
    JSONB,
    LINE_QTY,
    MONEY,
    NOW,
    QTY,
    RATING,
    RATIO,
    TEXT,
    TEXT_ARRAY,
    TIMESTAMPTZ,
    UNIT_MONEY,
    UUID,
    YIELD,
    ck,
    create_extension_if_absent,
    create_index_if_absent,
    create_table_if_absent,
    created_at_column,
    drop_index_if_present,
    drop_table_if_present,
    excl,
    fk,
    pk_column,
    pk_constraint,
    replace_constraint,
    slug_column,
    updated_at_column,
    uq,
)

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# PLURAL, deliberately, and the only plural table in the schema.
# `order` is a reserved word: CREATE TABLE order is a syntax error, and while
# "order" works when quoted, every hand-written query, every view in
# warehouse/models/, and every statement the warehouse MCP server's allowlisted
# SQL hands to an agent would need the quotes forever. An LLM writing
# FROM order fails every time. ARCHITECTURE.md section 5 to be updated to match.
TABLE = "orders"

CHANNELS = "'direct','lieferando','wolt','uber_eats','instagram','whatsapp','catering'"
# Superset of the live backend's received|confirmed|delivered|cancelled
# (dhaka-kacchi-connect/worker/schema.sql), plus aggregator states and
# 'refunded' - issue_refund exists in the section 4.4 policy file.
STATUSES = (
    "'received','confirmed','in_production','out_for_delivery','delivered','cancelled','refunded'"
)


def upgrade() -> None:
    # VAT: there is deliberately NO vat column. Dhaka Kacchi operates under the
    # Kleinunternehmerregelung (section 19 UStG), so no VAT is charged and
    # `gross` is genuinely the full revenue.
    #
    # REVISIT THIS AT THE THRESHOLD. Once VAT applies, German takeaway food is
    # 7% and non-alcoholic drinks are generally 19%, so a kacchi + borhani
    # basket is MIXED-RATE and cannot be represented at order level - it needs
    # order_line.vat_rate. Until then every margin figure computed from `gross`
    # would be optimistic by 7-19%, and the section 9 exit gate would pass while
    # being systematically wrong.
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        sa.Column("channel", TEXT, nullable=False),
        # The platform's own id - 'ord_<uuid>' on the direct channel. Not in the
        # original task list, but without it re-ingestion has nothing to
        # ON CONFLICT on and every daily run duplicates yesterday's orders, so
        # the exit gate would pass on doubled revenue. Section 5's payout_line
        # already assumes it (external_order_id).
        sa.Column("external_id", TEXT, nullable=False),
        # nullable: walk-ins, unresolved identity, and GDPR ON DELETE SET NULL
        sa.Column("customer_id", UUID, nullable=True),
        sa.Column("placed_at", TIMESTAMPTZ, nullable=False),  # BUSINESS time
        sa.Column("promised_at", TIMESTAMPTZ, nullable=True),  # direct: the Saturday slot
        sa.Column("delivered_at", TIMESTAMPTZ, nullable=True),
        sa.Column("gross", MONEY, nullable=False),
        sa.Column("discounts", MONEY, nullable=False, server_default=sa.text("0")),
        # aggregator commission - untrusted until reconciled against payout_line
        sa.Column("channel_fee", MONEY, nullable=False, server_default=sa.text("0")),
        sa.Column("packaging_cost", MONEY, nullable=False, server_default=sa.text("0")),
        sa.Column("delivery_cost", MONEY, nullable=False, server_default=sa.text("0")),
        # CACHE ONLY, recomputed by a job. The components are the truth; the
        # exit-gate query recomputes and reports drift rather than trusting this.
        sa.Column("net_margin", MONEY, nullable=True),
        sa.Column("status", TEXT, nullable=False, server_default=sa.text("'received'")),
        created_at_column(),  # WAREHOUSE landing time, deliberately != placed_at
        updated_at_column(),
        # Makes daily re-ingestion an upsert instead of a duplicate.
        sa.UniqueConstraint("channel", "external_id", name=uq(TABLE, "channel_external_id")),
        sa.CheckConstraint(f"channel IN ({CHANNELS})", name=ck(TABLE, "channel")),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=ck(TABLE, "status")),
        # net_margin MAY be negative - that is the entire point of measuring it.
        sa.CheckConstraint(
            "gross >= 0 AND discounts >= 0 AND channel_fee >= 0 "
            "AND packaging_cost >= 0 AND delivery_cost >= 0",
            name=ck(TABLE, "money"),
        ),
        sa.CheckConstraint(
            "promised_at IS NULL OR promised_at >= placed_at", name=ck(TABLE, "promised")
        ),
        sa.CheckConstraint(
            "delivered_at IS NULL OR delivered_at >= placed_at", name=ck(TABLE, "delivered")
        ),
        sa.CheckConstraint(
            "status <> 'delivered' OR delivered_at IS NOT NULL",
            name=ck(TABLE, "delivered_consistency"),
        ),
        # SET NULL, not CASCADE: a GDPR erasure must never be blocked by an FK,
        # and must never destroy revenue records (GoBD retention). The order
        # survives, anonymised. See section 10.
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customer.id"], name=fk(TABLE, "customer_id"), ondelete="SET NULL"
        ),
    )
    # The section 9 exit gate range-scans a month of placed_at.
    create_index_if_absent(TABLE, ["placed_at"], postgresql_ops={"placed_at": "DESC"})
    # FK child column: without this every GDPR customer delete seq-scans orders.
    # Also serves RFM / repeat-customer queries.
    create_index_if_absent(TABLE, ["customer_id"])


def downgrade() -> None:
    drop_table_if_present(TABLE)
