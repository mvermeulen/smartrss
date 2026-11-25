"""Milestone 1a core schema.

Revision ID: 0001_milestone_1a_core
Revises:
Create Date: 2026-11-25 00:00:00+00:00
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_milestone_1a_core"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    val = int(raw.strip())
    if val < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {val}")
    return val


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # 1. devices
    op.create_table(
        "devices",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("platform IN ('ubuntu', 'android', 'web')", name="ck_devices_platform"),
        sa.UniqueConstraint("token_hash", name="uq_devices_token_hash"),
    )

    # 2. pairing_codes
    op.create_table(
        "pairing_codes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("device_name", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False, server_default=sa.text("'ubuntu'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "platform IN ('ubuntu', 'android', 'web')", name="ck_pairing_codes_platform"
        ),
        sa.UniqueConstraint("code_hash", name="uq_pairing_codes_code_hash"),
    )

    # 3. sync_state
    op.create_table(
        "sync_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("current_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "instance_epoch",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1", name="ck_sync_state_singleton"),
    )
    op.execute("INSERT INTO sync_state (id, current_seq) VALUES (1, 0)")

    # 4. sync_changes
    op.create_table(
        "sync_changes",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("op IN ('upsert', 'delete')", name="ck_sync_changes_op"),
    )
    op.create_index("ix_sync_changes_entity", "sync_changes", ["entity_type", "entity_id"])
    op.create_index("ix_sync_changes_changed_at", "sync_changes", ["changed_at"])

    # 5. categories
    op.create_table(
        "categories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_categories_active_name",
        "categories",
        [sa.text("LOWER(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # 6. feeds
    op.create_table(
        "feeds",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("feed_url", sa.Text(), nullable=False),
        sa.Column("site_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("health_state", sa.Text(), nullable=False, server_default=sa.text("'healthy'")),
        sa.Column(
            "poll_interval_minutes", sa.Integer(), nullable=False, server_default=sa.text("60")
        ),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_fetch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "initial_backfill_done", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column("expiration_days_override", sa.Integer(), nullable=True),
        sa.Column("fetch_full_text", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column(
            "research_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(
                '\'{"enabled": false, "fetch_pdfs": false, "guidance": "", "max_hops": 1}\'::jsonb'
            ),
        ),
        sa.Column("curation_instructions", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "field_updated_at",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("feed_url", name="uq_feeds_feed_url"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'muted', 'unsubscribed')", name="ck_feeds_status"
        ),
        sa.CheckConstraint(
            "health_state IN ('healthy', 'degraded', 'broken', 'gone')",
            name="ck_feeds_health_state",
        ),
        sa.CheckConstraint("poll_interval_minutes >= 5", name="ck_feeds_poll_interval_minutes"),
        sa.CheckConstraint(
            "expiration_days_override IS NULL OR expiration_days_override >= 0",
            name="ck_feeds_expiration_days_override",
        ),
    )
    op.create_index(
        "ix_feeds_due_poll",
        "feeds",
        ["next_fetch_at"],
        postgresql_where=sa.text("status IN ('active', 'muted') AND health_state <> 'gone'"),
    )

    # 7. items
    op.create_table(
        "items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "feed_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("feeds.id"), nullable=False
        ),
        sa.Column("guid", sa.Text(), nullable=False),
        sa.Column("raw_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_summary", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column(
            "extraction_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("entry_hash", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'unread'")),
        sa.Column("is_starred", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("starred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_state_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("starred_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unexpired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("feed_id", "guid", name="uq_items_feed_guid"),
        sa.CheckConstraint(
            "extraction_status IN ('pending', 'ok', 'fallback_rss', 'failed')",
            name="ck_items_extraction_status",
        ),
        sa.CheckConstraint("state IN ('unread', 'read', 'expired')", name="ck_items_state"),
    )
    op.create_index("ix_items_feed_canonical_url", "items", ["feed_id", "canonical_url"])
    op.create_index("ix_items_canonical_url", "items", ["canonical_url"])
    op.create_index(
        "ix_items_expiration",
        "items",
        [sa.text("COALESCE(unexpired_at, fetched_at)")],
        postgresql_where=sa.text("state = 'unread' AND is_starred = FALSE"),
    )
    op.create_index("ix_items_feed_fetched", "items", ["feed_id", sa.text("fetched_at DESC")])

    # 8. pipeline_runs
    op.create_table(
        "pipeline_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("stages", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("items_polled", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("items_extracted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("items_clustered", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("items_researched", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("items_enriched", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("jobs_failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_llm_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('scheduler_window', 'cron_cli', 'manual')",
            name="ck_pipeline_runs_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed')",
            name="ck_pipeline_runs_status",
        ),
    )

    # 9. jobs
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "feed_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("feeds.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default=sa.text("50")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queue_wait_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name="ck_jobs_status"
        ),
    )
    op.create_index(
        "uq_jobs_active_dedupe",
        "jobs",
        ["job_type", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_jobs_claim",
        "jobs",
        ["priority", "run_after", "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_jobs_running_lease",
        "jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_jobs_done_finished",
        "jobs",
        ["finished_at"],
        postgresql_where=sa.text("status = 'done'"),
    )

    # 10. stage_metrics
    op.create_table(
        "stage_metrics",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("feed_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("prompt_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("queue_wait_ms", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("llm_prompt_ms", sa.Integer(), nullable=True),
        sa.Column("llm_gen_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("readiness_lag_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("status IN ('ok', 'failed', 'skipped')", name="ck_stage_metrics_status"),
    )
    op.create_index(
        "ix_stage_metrics_job_type_created", "stage_metrics", ["job_type", "created_at"]
    )
    op.create_index("ix_stage_metrics_feed_created", "stage_metrics", ["feed_id", "created_at"])

    # 11. app_settings
    op.create_table(
        "app_settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column(
            "default_expiration_days", sa.Integer(), nullable=False, server_default=sa.text("30")
        ),
        sa.Column("backfill_cap", sa.Integer(), nullable=False, server_default=sa.text("10")),
        sa.Column(
            "default_poll_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column("last_retention_cleanup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1", name="ck_app_settings_singleton"),
        sa.CheckConstraint("default_expiration_days >= 0", name="ck_app_settings_expiration"),
        sa.CheckConstraint("backfill_cap >= 0", name="ck_app_settings_backfill_cap"),
        sa.CheckConstraint(
            "default_poll_interval_minutes >= 5", name="ck_app_settings_poll_interval"
        ),
    )

    exp_days = _env_int("DEFAULT_EXPIRATION_DAYS", 30, 0)
    backfill_cap = _env_int("DEFAULT_BACKFILL_CAP", 10, 0)
    poll_interval = _env_int("DEFAULT_POLL_INTERVAL_MINUTES", 60, 5)
    op.execute(
        sa.text(
            "INSERT INTO app_settings (id, default_expiration_days, backfill_cap, default_poll_interval_minutes) "
            "VALUES (1, :exp_days, :backfill_cap, :poll_interval)"
        ).bindparams(
            exp_days=exp_days,
            backfill_cap=backfill_cap,
            poll_interval=poll_interval,
        )
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_index("ix_stage_metrics_feed_created", table_name="stage_metrics")
    op.drop_index("ix_stage_metrics_job_type_created", table_name="stage_metrics")
    op.drop_table("stage_metrics")
    op.drop_index("ix_jobs_done_finished", table_name="jobs")
    op.drop_index("ix_jobs_running_lease", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_index("uq_jobs_active_dedupe", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("pipeline_runs")
    op.drop_index("ix_items_feed_fetched", table_name="items")
    op.drop_index("ix_items_expiration", table_name="items")
    op.drop_index("ix_items_canonical_url", table_name="items")
    op.drop_index("ix_items_feed_canonical_url", table_name="items")
    op.drop_table("items")
    op.drop_index("ix_feeds_due_poll", table_name="feeds")
    op.drop_table("feeds")
    op.drop_index("uq_categories_active_name", table_name="categories")
    op.drop_table("categories")
    op.drop_index("ix_sync_changes_changed_at", table_name="sync_changes")
    op.drop_index("ix_sync_changes_entity", table_name="sync_changes")
    op.drop_table("sync_changes")
    op.drop_table("sync_state")
    op.drop_table("pairing_codes")
    op.drop_table("devices")
