"""User-test metrics columns (304 outcome, http_status, feed poll counters) and URL recanonicalization.

Revision ID: 0003_user_test_metrics_canon
Revises: 0002_milestone_1b_sync_search
Create Date: 2026-11-27 14:15:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from smartrss.db.recanonicalize import compute_updated_canonical_url

# revision identifiers, used by Alembic.
revision: str = "0003_user_test_metrics_canon"
down_revision: str | Sequence[str] | None = "0002_milestone_1b_sync_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_SIZE = 500


def upgrade() -> None:
    # 1. E-08: stage_metrics.outcome + http_status (keeping status='ok' for 304 Not Modified)
    op.add_column("stage_metrics", sa.Column("outcome", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_stage_metrics_outcome",
        "stage_metrics",
        "outcome IS NULL OR outcome IN ('fetched', 'not_modified')",
    )
    op.add_column("stage_metrics", sa.Column("http_status", sa.SmallInteger(), nullable=True))

    # 2. E-06 / E-08: pipeline_runs feed poll & 304 counters
    op.add_column(
        "pipeline_runs",
        sa.Column(
            "feeds_polled",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column(
            "feeds_not_modified",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # 3. E-12: Re-canonicalize existing items.canonical_url (e.g. strip BBC at_medium/at_campaign)
    # and emit item upserts into sync_changes under the serialized sync_state lock.
    bind = op.get_bind()
    last_id = None
    changed_ids: list[object] = []
    now = datetime.now(UTC)

    while True:
        if last_id is None:
            rows = bind.execute(
                sa.text(
                    """
                    SELECT id, raw_url, canonical_url
                    FROM items
                    ORDER BY id ASC
                    LIMIT :batch_size
                    """
                ),
                {"batch_size": _BATCH_SIZE},
            ).fetchall()
        else:
            rows = bind.execute(
                sa.text(
                    """
                    SELECT id, raw_url, canonical_url
                    FROM items
                    WHERE id > :last_id
                    ORDER BY id ASC
                    LIMIT :batch_size
                    """
                ),
                {"last_id": last_id, "batch_size": _BATCH_SIZE},
            ).fetchall()

        if not rows:
            break

        last_id = rows[-1].id
        updates: list[dict[str, object]] = []
        for row in rows:
            new_canon = compute_updated_canonical_url(row.raw_url, row.canonical_url)
            if new_canon is not None:
                updates.append({"item_id": row.id, "canonical_url": new_canon, "updated_at": now})
                changed_ids.append(row.id)

        if updates:
            bind.execute(
                sa.text(
                    """
                    UPDATE items
                    SET canonical_url = :canonical_url, updated_at = :updated_at
                    WHERE id = :item_id
                    """
                ),
                updates,
            )

    if changed_ids:
        current_seq = bind.execute(
            sa.text("SELECT current_seq FROM sync_state WHERE id = 1 FOR UPDATE")
        ).scalar_one()
        new_seq = int(current_seq) + len(changed_ids)
        bind.execute(
            sa.text("UPDATE sync_state SET current_seq = :new_seq, updated_at = :now WHERE id = 1"),
            {"new_seq": new_seq, "now": now},
        )
        for offset in range(0, len(changed_ids), _BATCH_SIZE):
            chunk = changed_ids[offset : offset + _BATCH_SIZE]
            sync_params = [
                {
                    "seq": int(current_seq) + offset + idx + 1,
                    "entity_type": "item",
                    "entity_id": item_id,
                    "op": "upsert",
                    "changed_at": now,
                }
                for idx, item_id in enumerate(chunk)
            ]
            bind.execute(
                sa.text(
                    """
                    INSERT INTO sync_changes (seq, entity_type, entity_id, op, changed_at)
                    VALUES (:seq, :entity_type, :entity_id, :op, :changed_at)
                    """
                ),
                sync_params,
            )


def downgrade() -> None:
    op.drop_column("pipeline_runs", "feeds_not_modified")
    op.drop_column("pipeline_runs", "feeds_polled")
    op.drop_column("stage_metrics", "http_status")
    op.drop_constraint("ck_stage_metrics_outcome", "stage_metrics", type_="check")
    op.drop_column("stage_metrics", "outcome")
