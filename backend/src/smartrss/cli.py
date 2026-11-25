"""SmartRSS command-line interface (smartrss, §10.2)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
import typer
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession

from smartrss.config import get_settings
from smartrss.db.engine import build_engine, build_sessionmaker
from smartrss.db.models import AppSettings, Device, SyncState
from smartrss.db.sync_log import APP_SETTINGS_ENTITY_ID, SyncEvent, sync_log
from smartrss.logging import configure_logging
from smartrss.security.auth import create_pairing_code, revoke_device
from smartrss.security.safe_http import SafeHttpClient, SSRFPolicy
from smartrss.worker.runner import run_worker_daemon, run_worker_once

app = typer.Typer(name="smartrss", no_args_is_help=True)
pair_app = typer.Typer(help="Device pairing commands", no_args_is_help=True)
devices_app = typer.Typer(help="Device management commands", no_args_is_help=True)
worker_app = typer.Typer(help="Background worker commands", no_args_is_help=True)
settings_app = typer.Typer(help="Database app_settings commands", no_args_is_help=True)
db_app = typer.Typer(help="Database migration and operational commands", no_args_is_help=True)

app.add_typer(pair_app, name="pair")
app.add_typer(devices_app, name="devices")
app.add_typer(worker_app, name="worker")
app.add_typer(settings_app, name="settings")
app.add_typer(db_app, name="db")

EX_CONFIG = 78

VALID_SETTINGS_MIN: dict[str, int] = {
    "backfill_cap": 0,
    "default_expiration_days": 0,
    "default_poll_interval_minutes": 5,
}


def _find_alembic_ini() -> Path:
    candidates = [
        Path.cwd() / "alembic.ini",
        Path(__file__).resolve().parents[2] / "alembic.ini",
        Path("/app/alembic.ini"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("Could not locate alembic.ini")


@pair_app.command("create")
def pair_create(
    name: str = typer.Option(..., "--name", help="Human-readable device name"),
    platform: Literal["ubuntu", "android", "web"] = typer.Option(
        "ubuntu", "--platform", help="Client platform"
    ),
) -> None:
    """Generate a one-time 5-minute pairing code for a new device."""
    cfg = get_settings()

    async def _run() -> None:
        engine = build_engine(cfg.database_url)
        factory = build_sessionmaker(engine)
        try:
            async with factory() as session:
                created = await create_pairing_code(session, device_name=name, platform=platform)
                await session.commit()
            typer.echo(f"Pairing code: {created.code}")
            typer.echo(f"Device name:  {name} ({platform})")
            typer.echo(f"Expires at:   {created.expires_at.isoformat()}")
        finally:
            await engine.dispose()

    asyncio.run(_run())


@devices_app.command("list")
def devices_list() -> None:
    """List all paired and revoked devices."""
    cfg = get_settings()

    async def _run() -> None:
        engine = build_engine(cfg.database_url)
        factory = build_sessionmaker(engine)
        try:
            async with factory() as session:
                rows = (
                    (await session.execute(sa.select(Device).order_by(Device.created_at.asc())))
                    .scalars()
                    .all()
                )
            if not rows:
                typer.echo("No devices paired.")
                return
            for d in rows:
                status_str = f"REVOKED ({d.revoked_at.isoformat()})" if d.revoked_at else "active"
                last_seen = d.last_seen_at.isoformat() if d.last_seen_at else "never"
                typer.echo(
                    f"{d.id}  {d.platform:<8}  {status_str:<32}  last_seen={last_seen}  {d.name}"
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


@devices_app.command("revoke")
def devices_revoke(
    device_id: uuid.UUID = typer.Argument(..., help="Device UUID to revoke"),
) -> None:
    """Revoke a device's bearer token immediately."""
    cfg = get_settings()

    async def _run() -> None:
        engine = build_engine(cfg.database_url)
        factory = build_sessionmaker(engine)
        try:
            async with factory() as session:
                revoked = await revoke_device(session, device_id)
                if revoked is None:
                    typer.echo(f"Device not found: {device_id}", err=True)
                    raise typer.Exit(code=1)
                await session.commit()
            typer.echo(f"Revoked device {revoked.id} ({revoked.name})")
        finally:
            await engine.dispose()

    asyncio.run(_run())


@worker_app.command("run")
def worker_run() -> None:
    """Run the continuous background worker daemon (refuses to start if WORKER_MODE=cron)."""
    cfg = get_settings()
    if cfg.worker_mode == "cron":
        typer.echo(
            "Refusing to start continuous worker daemon because WORKER_MODE=cron. "
            "Use `smartrss worker run-once` from host cron or set WORKER_MODE=daemon.",
            err=True,
        )
        raise typer.Exit(code=EX_CONFIG)

    configure_logging(cfg.log_level)

    async def _run() -> None:
        engine = build_engine(cfg.database_url, strict_sync_guard=cfg.synclog_strict_guard)
        factory = build_sessionmaker(engine, strict_sync_guard=cfg.synclog_strict_guard)
        policy = SSRFPolicy(extra_blocked_cidrs=cfg.ssrf_blocked_cidrs)
        async with SafeHttpClient(policy=policy) as http_client:
            try:
                await run_worker_daemon(
                    factory,
                    http_client,
                    concurrency=cfg.worker_io_concurrency,
                    poll_interval_seconds=float(cfg.worker_poll_interval_seconds),
                )
            finally:
                await engine.dispose()

    asyncio.run(_run())


@worker_app.command("run-once")
def worker_run_once_cmd(
    stage: Literal["all", "poll", "extract", "expire", "maintenance"] = typer.Option(
        "all", "--stage", help="Pipeline stage(s) to run"
    ),
    feed: uuid.UUID | None = typer.Option(None, "--feed", help="Restrict to single feed UUID"),
    force_poll: bool = typer.Option(False, "--force-poll", help="Ignore next_fetch_at"),
    max_duration: int = typer.Option(
        1800, "--max-duration", min=1, help="Max wall-clock duration in seconds"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON summary to stdout"),
) -> None:
    """Run one cron-friendly batch pass (exit 0=completed, 2=partial, 1=failed)."""
    cfg = get_settings()
    if not json_output:
        configure_logging(cfg.log_level)

    async def _run() -> int:
        engine = build_engine(cfg.database_url, strict_sync_guard=cfg.synclog_strict_guard)
        factory = build_sessionmaker(engine, strict_sync_guard=cfg.synclog_strict_guard)
        policy = SSRFPolicy(extra_blocked_cidrs=cfg.ssrf_blocked_cidrs)
        async with SafeHttpClient(policy=policy) as http_client:
            try:
                summary = await run_worker_once(
                    engine,
                    factory,
                    http_client,
                    stage=stage,
                    feed_id=feed,
                    force_poll=force_poll,
                    max_duration_seconds=float(max_duration),
                    concurrency=cfg.worker_io_concurrency,
                )
            finally:
                await engine.dispose()

        if json_output:
            typer.echo(json.dumps(summary.to_dict()))
        else:
            typer.echo(
                f"status={summary.status} duration_ms={summary.duration_ms} "
                f"polled={summary.items_polled} extracted={summary.items_extracted} "
                f"failed={summary.jobs_failed} deferred={summary.jobs_deferred}"
            )
        return summary.exit_code

    exit_code = asyncio.run(_run())
    raise typer.Exit(code=exit_code)


@settings_app.command("get")
def settings_get() -> None:
    """Print current authoritative database app_settings."""
    cfg = get_settings()

    async def _run() -> None:
        engine = build_engine(cfg.database_url)
        factory = build_sessionmaker(engine)
        try:
            async with factory() as session:
                row = await session.get(AppSettings, 1)
                if row is None:
                    typer.echo("app_settings row not initialized", err=True)
                    raise typer.Exit(code=1)
                typer.echo(
                    json.dumps(
                        {
                            "backfill_cap": row.backfill_cap,
                            "default_expiration_days": row.default_expiration_days,
                            "default_poll_interval_minutes": row.default_poll_interval_minutes,
                            "last_retention_cleanup_at": (
                                row.last_retention_cleanup_at.isoformat()
                                if row.last_retention_cleanup_at
                                else None
                            ),
                            "updated_at": row.updated_at.isoformat(),
                        },
                        indent=2,
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


@settings_app.command("set")
def settings_set(
    key: str = typer.Argument(
        ...,
        help="Setting key: backfill_cap | default_expiration_days | default_poll_interval_minutes",
    ),
    value: int = typer.Argument(..., help="Integer value"),
) -> None:
    """Update a database app_settings key and emit an app_settings sync event."""
    if key not in VALID_SETTINGS_MIN:
        typer.echo(
            f"Unsupported key {key!r}. Allowed: {', '.join(sorted(VALID_SETTINGS_MIN))}",
            err=True,
        )
        raise typer.Exit(code=2)
    min_val = VALID_SETTINGS_MIN[key]
    if value < min_val:
        typer.echo(f"{key} must be >= {min_val}, got {value}", err=True)
        raise typer.Exit(code=2)

    cfg = get_settings()

    async def _run() -> None:
        engine = build_engine(cfg.database_url, strict_sync_guard=cfg.synclog_strict_guard)
        factory = build_sessionmaker(engine, strict_sync_guard=cfg.synclog_strict_guard)
        try:
            async with factory() as session:
                await update_app_setting(session, key=key, value=value)
                await session.commit()
            typer.echo(f"Updated {key}={value}")
        finally:
            await engine.dispose()

    asyncio.run(_run())


async def update_app_setting(
    session: AsyncSession,
    *,
    key: str,
    value: int,
) -> AppSettings:
    """Update an app_settings column, stamp updated_at, and emit app_settings sync upsert."""
    if key not in VALID_SETTINGS_MIN:
        raise ValueError(f"Unsupported setting key: {key!r}")
    if value < VALID_SETTINGS_MIN[key]:
        raise ValueError(f"{key} must be >= {VALID_SETTINGS_MIN[key]}")

    row = await session.get(AppSettings, 1)
    if row is None:
        raise RuntimeError("app_settings singleton row (id=1) is missing")

    setattr(row, key, value)
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await sync_log.record(session, [SyncEvent("app_settings", APP_SETTINGS_ENTITY_ID, "upsert")])
    return row


@db_app.command("migrate")
def db_migrate(
    database_url: str | None = typer.Option(None, "--database-url", help="Override DATABASE_URL"),
) -> None:
    """Apply Alembic migrations up to head."""
    ini_path = _find_alembic_ini()
    alembic_cfg = AlembicConfig(str(ini_path))
    alembic_cfg.set_main_option("script_location", str(ini_path.parent / "alembic"))
    url = database_url or os.environ.get("DATABASE_URL") or get_settings().database_url
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.upgrade(alembic_cfg, "head")
    typer.echo("Database migrated to head.")


@db_app.command("rotate-epoch")
def db_rotate_epoch(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="Explicit target database URL (mutually exclusive with --database-url-env)",
    ),
    database_url_env: str | None = typer.Option(
        None,
        "--database-url-env",
        help="Name of env var holding the explicit target database URL (avoids argv exposure)",
    ),
) -> None:
    """Rotate sync_state.instance_epoch on the explicitly specified target database."""
    target_url: str | None = None
    if database_url and database_url_env:
        typer.echo("Pass either --database-url or --database-url-env, not both", err=True)
        raise typer.Exit(code=2)
    if database_url_env:
        target_url = os.environ.get(database_url_env, "").strip()
        if not target_url:
            typer.echo(f"Environment variable {database_url_env!r} is unset or empty", err=True)
            raise typer.Exit(code=2)
    elif database_url:
        target_url = database_url.strip()

    if not target_url:
        typer.echo("Either --database-url or --database-url-env is required", err=True)
        raise typer.Exit(code=2)

    async def _run() -> None:
        engine = build_engine(target_url)
        factory = build_sessionmaker(engine)
        try:
            async with factory() as session:
                state = (
                    await session.execute(
                        sa.select(SyncState).where(SyncState.id == 1).with_for_update()
                    )
                ).scalar_one()
                old_epoch = state.instance_epoch
                new_epoch = uuid.uuid4()
                while new_epoch == old_epoch:
                    new_epoch = uuid.uuid4()
                state.instance_epoch = new_epoch
                state.updated_at = datetime.now(UTC)
                await session.commit()
            typer.echo(f"Rotated instance_epoch: {old_epoch} -> {new_epoch}")
        finally:
            await engine.dispose()

    asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(app())
