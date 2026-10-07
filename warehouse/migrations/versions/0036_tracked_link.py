"""tracked_link

Revision ID: 0036
Revises: 0035

The tagged links created in the website's admin Link builder: raw_tracked_links (the
ordering database's row as read) and tracked_link (typed), the bridge from a SHARED
LINK to the POST that carried it.

WHY THIS TABLE EXISTS. A website visit arrives with utm_source / utm_content, and
event.ingest resolves those against channel.platform + campaign_variant.utm_content.
Until now the only variant that existed was one catch-all "bio_link" per platform, so a
visit could be traced to "Instagram" but never to WHICH post. warehouse/ingest/links.py
now gives every link its own campaign_variant (campaign_variant_id below), which is what
lets a visit - and the order it led to - be joined to one post.

social_post_id is the join to the post itself. It is filled from post_url (the person
pastes the published post's address into the Link builder AFTER publishing, because the
link has to exist before the post that carries it) by matching social_post.permalink.
Both sides are normalised the same way (https, no "www.", no query/fragment, no trailing
slash). NULL until the post is attached AND ingested; a link for a creator or a WhatsApp
broadcast has no social_post and never will - that is not an error.

(source, content) is UNIQUE here because the source database enforces it and because it
is exactly the pair a visit is resolved by.

campaign_variant_id is RESTRICT, like event's own FK to it: a variant that visits were
attributed to is historical evidence. NULL when the link could not be given a variant -
see links.py for the one case (a clash with an existing variant for the same platform
and content), which is reported, not hidden.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    JSONB,
    TEXT,
    TIMESTAMPTZ,
    UUID,
    create_index_if_absent,
    create_table_if_absent,
    created_at_column,
    drop_table_if_present,
    fk,
    pk_column,
    pk_constraint,
    updated_at_column,
    uq,
)

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RAW = "raw_tracked_links"
LINK = "tracked_link"


def upgrade() -> None:
    create_table_if_absent(
        RAW,
        pk_column(),
        pk_constraint(RAW),
        sa.Column("external_id", TEXT, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("external_id", name=uq(RAW, "external_id")),
    )

    create_table_if_absent(
        LINK,
        pk_column(),
        pk_constraint(LINK),
        sa.Column("external_id", TEXT, nullable=False),
        sa.Column("label", TEXT, nullable=False),
        sa.Column("source", TEXT, nullable=False),
        sa.Column("medium", TEXT, nullable=False),
        sa.Column("campaign", TEXT, nullable=False),
        sa.Column("content", TEXT, nullable=False),
        sa.Column("destination_path", TEXT, nullable=False),
        sa.Column("url", TEXT, nullable=False),
        sa.Column("post_url", TEXT, nullable=True),
        sa.Column("social_post_id", UUID, nullable=True),
        sa.Column("campaign_variant_id", UUID, nullable=True),
        sa.Column("link_created_at", TIMESTAMPTZ, nullable=False),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("external_id", name=uq(LINK, "external_id")),
        sa.UniqueConstraint("source", "content", name=uq(LINK, "source_content")),
        sa.ForeignKeyConstraint(
            ["social_post_id"],
            ["social_post.id"],
            name=fk(LINK, "social_post_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_variant_id"],
            ["campaign_variant.id"],
            name=fk(LINK, "campaign_variant_id"),
            ondelete="RESTRICT",
        ),
    )
    create_index_if_absent(LINK, ["social_post_id"], where="social_post_id IS NOT NULL")
    create_index_if_absent(LINK, ["campaign_variant_id"], where="campaign_variant_id IS NOT NULL")


def downgrade() -> None:
    drop_table_if_present(LINK)
    drop_table_if_present(RAW)
