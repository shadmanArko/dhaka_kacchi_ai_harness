"""social_metrics_nullable

Revision ID: 0030
Revises: 0029

Makes the seven original metric columns on social_metrics_snapshot
(impressions, reach, likes, comments, shares, saves, clicks) NULLABLE with NO
default, so a source can say "this platform did not report that metric".

WHY: 0022 declared them NOT NULL DEFAULT 0, which cannot tell a real zero from
a gap, and the warehouse already holds that exact ambiguity - every Instagram
post stores impressions = 0, every Threads post stores reach = 0 and saves = 0,
only Facebook has clicks. Those zeros were hardcoded by the ingesters because
the column forced a value. YouTube's Data API returns views/likes/comments but
no impressions, reach, shares, saves or clicks; without this migration its
ingester would have to write five invented zeros per video.

WHAT IS DELIBERATELY NOT DONE: existing rows are NOT rewritten. The zeros
already stored for Instagram/Threads/Facebook stay zeros. Converting them to
NULL would change data that the engagement predictor, the admin reports and
the social_share product were all built against, and "which zeros are real"
needs a per-platform decision this migration should not make silently. It is an
optional, separate backfill.

THE DEFAULT IS DROPPED TOO, not just NOT NULL. Leaving DEFAULT 0 in place would
make an ingester that merely OMITS a column write a silent 0 - the very bug
this fixes, reintroduced by forgetting an argument. Every existing ingester
(instagram/facebook/threads) passes all seven explicitly, so nothing changes
for them.

CONSUMERS CHECKED before this was written (a NULL where a number used to be is
a behaviour change for every reader):
  - ck_social_metrics_snapshot_nonneg: NULL passes a CHECK, so it still guards
    non-negativity without rejecting NULL.
  - ml/01-data/extract.py: hard-filters to facebook/instagram, whose rows stay
    non-null.
  - dhaka-kacchi-connect reportingRepository.ts: already coalesce(sum(), 0) and
    `?? 0`, NULL-safe.
  - ops/detectors/social_engagement_drop.py: `likes + comments + shares` would
    become NULL if shares is NULL - fixed in the same change with COALESCE.
  - ops/refresh_social_share.py: the shared table is NOT NULL and lives in a
    one-time init script this migration cannot alter - fixed in the same change
    with an explicit platform allowlist plus COALESCE.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "social_metrics_snapshot"
COLUMNS = ("impressions", "reach", "likes", "comments", "shares", "saves", "clicks")


def upgrade() -> None:
    for col in COLUMNS:
        op.alter_column(TABLE, col, nullable=True, server_default=None)


def downgrade() -> None:
    # Converges back to 0022's definition. NULLs must go before NOT NULL can be
    # re-applied, so they are coerced to 0 - lossy by necessity: it collapses
    # "not reported" back into "zero", which is exactly the ambiguity this
    # migration exists to remove. Only reach for this on a database that has
    # not yet ingested a source that depends on NULL.
    for col in COLUMNS:
        op.execute(f"UPDATE {TABLE} SET {col} = 0 WHERE {col} IS NULL")
        op.alter_column(TABLE, col, nullable=False, server_default="0")
