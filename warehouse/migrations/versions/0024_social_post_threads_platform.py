"""social_post_threads_platform

Revision ID: 0024
Revises: 0023

Widens social_post's platform CHECK to allow 'threads', alongside the
existing 'instagram'/'facebook'. 0021 is already committed/pushed, so this
is a follow-up migration rather than an edit to it - same pattern as 0015's
event.external_id addition.
"""

from __future__ import annotations

from collections.abc import Sequence

from warehouse.migrations.helpers import ck, replace_constraint

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "social_post"
CONSTRAINT = ck(TABLE, "platform")
PLATFORMS = "'instagram','facebook','threads'"


def upgrade() -> None:
    replace_constraint(TABLE, CONSTRAINT, f"CHECK (platform IN ({PLATFORMS}))")


def downgrade() -> None:
    # Converges back to the 0021 definition - drop-then-add, same mechanism.
    replace_constraint(TABLE, CONSTRAINT, "CHECK (platform IN ('instagram','facebook'))")
