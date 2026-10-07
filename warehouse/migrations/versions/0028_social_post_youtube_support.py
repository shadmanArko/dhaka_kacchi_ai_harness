"""social_post_youtube_support

Revision ID: 0028
Revises: 0027

Lets social_post hold YouTube content. Widens TWO closed vocabularies, not
one - the second was missed in planning and caught only because an insert
test tripped it:

  - platform:     + 'youtube'  (same follow-up-migration pattern as 0024;
                  0021 is already committed/pushed so it is not edited)
  - content_type: + 'short'    (a YouTube Short is a distinct format. Mapping
                  it onto 'reel' would be wrong: 'reel' is Instagram's own
                  term, and the two platforms' short-form formats are not
                  interchangeable for any analysis that compares them.)

A long-form YouTube video maps to the existing 'video'.

Deliberately NOT touched: channel.platform, which 0017 leaves unconstrained on
purpose (the set of acquisition platforms is open-ended, unlike the set this
warehouse has an ingest job for), and ad_spend's platform list - nothing is
spent on YouTube ads, so widening it would claim a data source that does not
exist.

Downstream, checked rather than assumed: ml/01-data/extract.py hard-filters to
platform IN ('facebook','instagram'), so YouTube rows never reach the
engagement predictor, and its encoders use handle_unknown='ignore'. If YouTube
ever does feed that model, ml/03-modeling/features.py's is_video
(isin(['video','reel'])) must learn about 'short'.
"""

from __future__ import annotations

from collections.abc import Sequence

from warehouse.migrations.helpers import ck, replace_constraint

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "social_post"
PLATFORM_CK = ck(TABLE, "platform")
CONTENT_TYPE_CK = ck(TABLE, "content_type")

PLATFORMS = "'instagram','facebook','threads','youtube'"
CONTENT_TYPES = "'image','video','carousel','reel','story','short'"


def upgrade() -> None:
    replace_constraint(TABLE, PLATFORM_CK, f"CHECK (platform IN ({PLATFORMS}))")
    replace_constraint(
        TABLE,
        CONTENT_TYPE_CK,
        f"CHECK (content_type IS NULL OR content_type IN ({CONTENT_TYPES}))",
    )


def downgrade() -> None:
    # Converges back to the 0024 / 0021 definitions. Postgres will (correctly)
    # refuse this once real 'youtube' / 'short' rows exist - the same property
    # 0024's own downgrade has for 'threads', and the reason CLAUDE.md warns
    # against running a full downgrade against a dev database holding real
    # ingests.
    replace_constraint(TABLE, PLATFORM_CK, "CHECK (platform IN ('instagram','facebook','threads'))")
    replace_constraint(
        TABLE,
        CONTENT_TYPE_CK,
        "CHECK (content_type IS NULL OR content_type IN "
        "('image','video','carousel','reel','story'))",
    )
