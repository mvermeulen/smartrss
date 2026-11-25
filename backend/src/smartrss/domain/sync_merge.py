"""Pure LWW conflict resolution, clock-skew clamping, status helpers, and projections (§6.5)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from smartrss.domain.fts_text import strip_html_to_plain_text

MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
MAX_PAST_CLOCK_SKEW = timedelta(days=90)
MAX_RAW_SUMMARY_CHARS = 32_768
LEAD_EXCERPT_CHARS = 1_500

STATE_RANK: dict[str, int] = {
    "expired": 1,
    "unread": 2,
    "read": 3,
}
BOOL_RANK: dict[bool, int] = {
    False: 1,
    True: 2,
}
FEED_STATUS_RANK: dict[str, int] = {
    "active": 1,
    "paused": 2,
    "muted": 3,
    "unsubscribed": 4,
}

MergeOutcome = Literal["applied", "superseded"]

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ItemStateSnapshot:
    """Pure value object representing an item's read/unread/expired state fields."""

    state: str
    read_at: datetime | None
    expired_at: datetime | None
    unexpired_at: datetime | None
    read_state_updated_at: datetime


@dataclass(frozen=True, slots=True)
class ItemStarSnapshot:
    """Pure value object representing an item's star fields (orthogonal to state)."""

    is_starred: bool
    starred_at: datetime | None
    starred_updated_at: datetime


def normalize_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware and convert it to UTC."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    return dt.astimezone(UTC)


def clamp_client_timestamp(
    client_ts: datetime,
    server_now: datetime,
) -> tuple[datetime | None, str | None]:
    """Validate dead-RTC lower bound (-90d) and clamp future skew (+5m) against server_now."""
    client_utc = normalize_utc(client_ts)
    server_utc = normalize_utc(server_now)
    if client_utc < server_utc - MAX_PAST_CLOCK_SKEW:
        return None, "clock_skew"
    max_allowed = server_utc + MAX_FUTURE_CLOCK_SKEW
    return min(client_utc, max_allowed), None


def compute_online_lww_timestamp(
    db_now: datetime,
    stored_ts: datetime | None,
) -> datetime:
    """Return strictly monotonic online LWW timestamp >= db_now and > stored_ts (P-02, C7)."""
    db_utc = normalize_utc(db_now)
    if stored_ts is not None:
        stored_utc = normalize_utc(stored_ts)
        if stored_utc >= db_utc:
            return stored_utc + timedelta(microseconds=1)
    return db_utc


def parse_field_timestamp(
    field_updated_at: dict[str, str] | None,
    field: str,
    fallback: datetime,
) -> datetime:
    """Look up a feed's per-field LWW timestamp, falling back to parent key or feed.created_at."""
    mapping = field_updated_at or {}
    raw = mapping.get(field)
    if raw is None and field.startswith("research_policy."):
        raw = mapping.get("research_policy")
    if raw is not None:
        try:
            return normalize_utc(datetime.fromisoformat(raw))
        except ValueError:
            pass
    return normalize_utc(fallback)


def compute_payload_sha256(payload: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hex digest of a canonical mutation payload (P-17)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cap_raw_summary(raw_summary: str | None) -> tuple[str | None, bool]:
    """Cap raw_summary to 32,768 chars and report whether truncation occurred (1b-D02)."""
    if raw_summary is None:
        return None, False
    if len(raw_summary) > MAX_RAW_SUMMARY_CHARS:
        return raw_summary[:MAX_RAW_SUMMARY_CHARS], True
    return raw_summary, False


def derive_lead_excerpt(
    *,
    extracted_text: str | None,
    raw_summary: str | None,
) -> str | None:
    """Build 1,500-char plain-text lead_excerpt from extracted_text or stripped raw_summary."""
    if extracted_text and extracted_text.strip():
        collapsed = _WHITESPACE_RE.sub(" ", extracted_text).strip()
        return collapsed[:LEAD_EXCERPT_CHARS] if collapsed else None
    if raw_summary:
        plain = strip_html_to_plain_text(raw_summary)
        return plain[:LEAD_EXCERPT_CHARS] if plain else None
    return None


def rank_feed_field_value(field: str, value: Any) -> Any:
    """Return a deterministic comparable tie-break rank for a feed_set_field value."""
    if field == "status":
        return FEED_STATUS_RANK[str(value)]
    if field == "category_id":
        return "" if value is None else str(value)
    if field == "expiration_days_override":
        return (-1,) if value is None else (int(value),)
    if field in ("research_policy.enabled", "research_policy.fetch_pdfs"):
        return BOOL_RANK[bool(value)]
    raise ValueError(f"Unsupported LWW feed field: {field!r}")


def decide_lww(
    *,
    incoming_value: Any,
    incoming_ts: datetime,
    incoming_rank: Any,
    current_value: Any,
    current_ts: datetime,
    current_rank: Any,
) -> MergeOutcome:
    """Deterministic value-rank LWW decision rule (§6.5 item 2, 1b-D04)."""
    in_ts = normalize_utc(incoming_ts)
    cur_ts = normalize_utc(current_ts)
    if incoming_value == current_value:
        return "applied" if in_ts > cur_ts else "superseded"
    if (in_ts, incoming_rank) > (cur_ts, current_rank):
        return "applied"
    return "superseded"


def merge_item_state(
    snapshot: ItemStateSnapshot,
    *,
    new_state: Literal["read", "unread"],
    effective_ts: datetime,
) -> tuple[MergeOutcome, ItemStateSnapshot]:
    """Apply pure LWW merge for items.state with order-independent side-effect timestamps."""
    eff_utc = normalize_utc(effective_ts)
    cur_ts = normalize_utc(snapshot.read_state_updated_at)
    outcome = decide_lww(
        incoming_value=new_state,
        incoming_ts=eff_utc,
        incoming_rank=STATE_RANK[new_state],
        current_value=snapshot.state,
        current_ts=cur_ts,
        current_rank=STATE_RANK[snapshot.state],
    )
    if outcome == "superseded":
        return "superseded", snapshot

    if new_state == "read":
        return (
            "applied",
            ItemStateSnapshot(
                state="read",
                read_at=eff_utc,
                expired_at=snapshot.expired_at,
                unexpired_at=None,
                read_state_updated_at=eff_utc,
            ),
        )

    # new_state == "unread": set unexpired_at whenever expired_at is present or state was expired
    # so concurrent read/unread permutations stay strictly order-independent (§6.5 item 3).
    next_unexpired = (
        eff_utc
        if (snapshot.state == "expired" or snapshot.expired_at is not None)
        else snapshot.unexpired_at
    )
    return (
        "applied",
        ItemStateSnapshot(
            state="unread",
            read_at=None,
            expired_at=snapshot.expired_at,
            unexpired_at=next_unexpired,
            read_state_updated_at=eff_utc,
        ),
    )


def merge_item_starred(
    snapshot: ItemStarSnapshot,
    *,
    new_starred: bool,
    effective_ts: datetime,
) -> tuple[MergeOutcome, ItemStarSnapshot]:
    """Apply pure LWW merge for items.is_starred (orthogonal to items.state)."""
    eff_utc = normalize_utc(effective_ts)
    cur_ts = normalize_utc(snapshot.starred_updated_at)
    outcome = decide_lww(
        incoming_value=new_starred,
        incoming_ts=eff_utc,
        incoming_rank=BOOL_RANK[new_starred],
        current_value=snapshot.is_starred,
        current_ts=cur_ts,
        current_rank=BOOL_RANK[snapshot.is_starred],
    )
    if outcome == "superseded":
        return "superseded", snapshot

    return (
        "applied",
        ItemStarSnapshot(
            is_starred=new_starred,
            starred_at=eff_utc if new_starred else None,
            starred_updated_at=eff_utc,
        ),
    )


def merge_feed_field(
    *,
    field: str,
    current_value: Any,
    stored_ts: datetime,
    new_value: Any,
    effective_ts: datetime,
) -> tuple[MergeOutcome, Any, datetime]:
    """Apply pure LWW merge for a single feed_set_field key."""
    eff_utc = normalize_utc(effective_ts)
    cur_ts = normalize_utc(stored_ts)
    outcome = decide_lww(
        incoming_value=new_value,
        incoming_ts=eff_utc,
        incoming_rank=rank_feed_field_value(field, new_value),
        current_value=current_value,
        current_ts=cur_ts,
        current_rank=rank_feed_field_value(field, current_value),
    )
    if outcome == "superseded":
        return "superseded", current_value, cur_ts
    return "applied", new_value, eff_utc


def apply_feed_status_transition(
    feed: Any,
    new_status: str,
    db_now: datetime,
) -> bool:
    """Apply shared feed status transition side effects for both online PATCH and offline push.

    Returns True when the caller must enqueue an immediate high-priority poll_feed job.
    """
    old_status = feed.status
    now_utc = normalize_utc(db_now)
    feed.status = new_status

    if old_status == "unsubscribed" and new_status == "active":
        feed.etag = None
        feed.last_modified = None
        feed.health_state = "healthy"
        feed.consecutive_errors = 0
        feed.last_error = None
        feed.initial_backfill_done = False
        feed.next_fetch_at = now_utc
        return True

    if old_status == "paused" and new_status in ("active", "muted"):
        if feed.next_fetch_at is None or normalize_utc(feed.next_fetch_at) > now_utc:
            feed.next_fetch_at = now_utc
        return False

    return False
