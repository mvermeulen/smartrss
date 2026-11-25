"""Milestone 1b sync engine and PostgreSQL full-text search schema.

Revision ID: 0002_milestone_1b_sync_search
Revises: 0001_milestone_1a_core
Create Date: 2026-11-25 12:00:00+00:00
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from smartrss.domain.fts_text import (
    MAX_FTS_BODY_CHARS,
    strip_html_to_plain_text,
    validate_fts_language,
)

# revision identifiers, used by Alembic.
revision: str = "0002_milestone_1b_sync_search"
down_revision: str | Sequence[str] | None = "0001_milestone_1a_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL_BATCH_SIZE = 500


def upgrade() -> None:
    # 1. sync_mutations idempotency log
    op.create_table(
        "sync_mutations",
        sa.Column("client_mutation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id"),
            nullable=False,
        ),
        sa.Column("mutation_type", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("client_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "server_applied_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("affected_count", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('applied', 'superseded', 'rejected')",
            name="ck_sync_mutations_outcome",
        ),
    )
    op.create_index(
        "ix_sync_mutations_applied_at",
        "sync_mutations",
        ["server_applied_at"],
    )

    # 2. sync_state.pruned_through_seq
    op.add_column(
        "sync_state",
        sa.Column(
            "pruned_through_seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_sync_state_pruned_through_seq_nonneg",
        "sync_state",
        "pruned_through_seq >= 0",
    )

    # 3. items.ingest_sync_seq + items.search_vector + indexes
    op.add_column(
        "items",
        sa.Column(
            "ingest_sync_seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_items_ingest_sync_seq_nonneg",
        "items",
        "ingest_sync_seq >= 0",
    )
    op.add_column(
        "items",
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
    )

    # 4. Backfill items.ingest_sync_seq from earliest remaining sync_changes row
    op.execute(
        sa.text(
            """
            UPDATE items
            SET ingest_sync_seq = COALESCE(sc.min_seq, 0)
            FROM (
                SELECT entity_id, MIN(seq) AS min_seq
                FROM sync_changes
                WHERE entity_type = 'item'
                GROUP BY entity_id
            ) AS sc
            WHERE items.id = sc.entity_id
            """
        )
    )

    # 5. Backfill items.search_vector in 500-row batches using shared Python HTMLParser helper
    fts_cfg = validate_fts_language(os.environ.get("FTS_LANGUAGE", "english"))
    bind = op.get_bind()
    last_id = None
    update_sql = sa.text(
        """
        UPDATE items
        SET search_vector =
            setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:title, '')), 'A') ||
            setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:author, '')), 'B') ||
            setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:summary_plain, '')), 'C') ||
            setweight(to_tsvector(CAST(:fts_cfg AS regconfig), COALESCE(:body_slice, '')), 'D')
        WHERE id = :item_id
        """
    )
    while True:
        if last_id is None:
            rows = bind.execute(
                sa.text(
                    """
                    SELECT id, title, author, raw_summary, LEFT(extracted_text, :body_cap) AS body_slice
                    FROM items
                    ORDER BY id ASC
                    LIMIT :batch_size
                    """
                ),
                {"body_cap": MAX_FTS_BODY_CHARS, "batch_size": _BACKFILL_BATCH_SIZE},
            ).fetchall()
        else:
            rows = bind.execute(
                sa.text(
                    """
                    SELECT id, title, author, raw_summary, LEFT(extracted_text, :body_cap) AS body_slice
                    FROM items
                    WHERE id > :last_id
                    ORDER BY id ASC
                    LIMIT :batch_size
                    """
                ),
                {
                    "last_id": last_id,
                    "body_cap": MAX_FTS_BODY_CHARS,
                    "batch_size": _BACKFILL_BATCH_SIZE,
                },
            ).fetchall()

        if not rows:
            break

        params = [
            {
                "fts_cfg": fts_cfg,
                "item_id": row.id,
                "title": row.title or "",
                "author": row.author or "",
                "summary_plain": strip_html_to_plain_text(row.raw_summary),
                "body_slice": row.body_slice or "",
            }
            for row in rows
        ]
        bind.execute(update_sql, params)
        last_id = rows[-1].id

    # 6. Create indexes after backfill
    op.create_index(
        "ix_items_unread_ingest_seq",
        "items",
        ["feed_id", "ingest_sync_seq"],
        postgresql_where=sa.text("state = 'unread'"),
    )
    op.create_index(
        "ix_items_search_vector",
        "items",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_items_bootstrap_unread_id",
        "items",
        ["id"],
        postgresql_where=sa.text("state = 'unread'"),
    )
    op.create_index(
        "ix_items_bootstrap_starred_id",
        "items",
        ["id"],
        postgresql_where=sa.text("is_starred = TRUE"),
    )
    op.create_index(
        "ix_items_bootstrap_read_at_id",
        "items",
        [sa.text("read_at DESC"), "id"],
        postgresql_where=sa.text("state = 'read'"),
    )


def downgrade() -> None:
    op.drop_index("ix_items_bootstrap_read_at_id", table_name="items")
    op.drop_index("ix_items_bootstrap_starred_id", table_name="items")
    op.drop_index("ix_items_bootstrap_unread_id", table_name="items")
    op.drop_index("ix_items_search_vector", table_name="items")
    op.drop_index("ix_items_unread_ingest_seq", table_name="items")
    op.drop_constraint("ck_items_ingest_sync_seq_nonneg", "items", type_="check")
    op.drop_column("items", "search_vector")
    op.drop_column("items", "ingest_sync_seq")

    op.drop_constraint("ck_sync_state_pruned_through_seq_nonneg", "sync_state", type_="check")
    op.drop_column("sync_state", "pruned_through_seq")

    op.drop_index("ix_sync_mutations_applied_at", table_name="sync_mutations")
    op.drop_table("sync_mutations")
