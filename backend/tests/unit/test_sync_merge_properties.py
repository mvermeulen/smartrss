"""Unit and Hypothesis property tests for pure LWW sync merge rules (1b-T02a)."""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from smartrss.domain.sync_merge import (
    LEAD_EXCERPT_CHARS,
    MAX_RAW_SUMMARY_CHARS,
    ItemStarSnapshot,
    ItemStateSnapshot,
    apply_feed_status_transition,
    cap_raw_summary,
    clamp_client_timestamp,
    compute_online_lww_timestamp,
    compute_payload_sha256,
    derive_lead_excerpt,
    merge_feed_field,
    merge_item_starred,
    merge_item_state,
    normalize_utc,
    parse_field_timestamp,
    rank_feed_field_value,
)

BASE_TIME = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


def _offset_ts(seconds: int) -> datetime:
    return BASE_TIME + timedelta(seconds=seconds)


@settings(max_examples=75, deadline=None)
@given(
    initial_state=st.sampled_from(["unread", "read", "expired"]),
    mutations=st.lists(
        st.tuples(
            st.sampled_from(["read", "unread"]),
            # Deliberately small timestamp domain so identical timestamps happen frequently
            st.integers(min_value=-10, max_value=10),
        ),
        min_size=1,
        max_size=5,
    ),
)
def test_1b_t02a_item_state_lww_commutes_across_all_permutations(
    initial_state: str,
    mutations: list[tuple[Literal["read", "unread"], int]],
) -> None:
    """Every permutation of item_set_state mutations (including ties) converges identically."""
    initial = ItemStateSnapshot(
        state=initial_state,
        read_at=BASE_TIME - timedelta(minutes=5) if initial_state == "read" else None,
        expired_at=BASE_TIME - timedelta(minutes=5) if initial_state == "expired" else None,
        unexpired_at=None,
        read_state_updated_at=BASE_TIME - timedelta(minutes=5),
    )

    def _run(order: tuple[tuple[Literal["read", "unread"], int], ...]) -> ItemStateSnapshot:
        snap = initial
        for state_val, sec in order:
            eff_ts, err = clamp_client_timestamp(_offset_ts(sec), BASE_TIME)
            assert err is None and eff_ts is not None
            _, snap = merge_item_state(snap, new_state=state_val, effective_ts=eff_ts)
        return snap

    perms = list(itertools.islice(itertools.permutations(mutations), 24))
    expected = _run(perms[0])
    for perm in perms[1:]:
        assert _run(perm) == expected

    # Applying duplicates of every mutation again is strictly idempotent
    replayed = expected
    for state_val, sec in mutations:
        eff_ts, _ = clamp_client_timestamp(_offset_ts(sec), BASE_TIME)
        assert eff_ts is not None
        outcome, replayed = merge_item_state(replayed, new_state=state_val, effective_ts=eff_ts)
        assert outcome == "superseded"
        assert replayed == expected


@settings(max_examples=75, deadline=None)
@given(
    initial_starred=st.booleans(),
    mutations=st.lists(
        st.tuples(st.booleans(), st.integers(min_value=-5, max_value=5)),
        min_size=1,
        max_size=5,
    ),
)
def test_1b_t02a_item_starred_lww_commutes_across_all_permutations(
    initial_starred: bool,
    mutations: list[tuple[bool, int]],
) -> None:
    initial = ItemStarSnapshot(
        is_starred=initial_starred,
        starred_at=BASE_TIME - timedelta(minutes=5) if initial_starred else None,
        starred_updated_at=BASE_TIME - timedelta(minutes=5),
    )

    def _run(order: tuple[tuple[bool, int], ...]) -> ItemStarSnapshot:
        snap = initial
        for starred, sec in order:
            eff_ts, _ = clamp_client_timestamp(_offset_ts(sec), BASE_TIME)
            assert eff_ts is not None
            _, snap = merge_item_starred(snap, new_starred=starred, effective_ts=eff_ts)
        return snap

    perms = list(itertools.islice(itertools.permutations(mutations), 24))
    expected = _run(perms[0])
    for perm in perms[1:]:
        assert _run(perm) == expected


@settings(max_examples=60, deadline=None)
@given(
    field=st.sampled_from(
        [
            "status",
            "category_id",
            "expiration_days_override",
            "research_policy.enabled",
            "research_policy.fetch_pdfs",
        ]
    ),
    timestamps=st.lists(st.integers(min_value=-5, max_value=600), min_size=2, max_size=5),
    choices=st.lists(st.integers(min_value=0, max_value=3), min_size=2, max_size=5),
)
def test_1b_t02a_feed_field_lww_commutes_including_clamped_future_ties(
    field: str,
    timestamps: list[int],
    choices: list[int],
) -> None:
    cat_a = uuid.UUID("00000000-0000-0000-0000-000000000010")
    cat_b = uuid.UUID("00000000-0000-0000-0000-000000000020")
    domain_values: dict[str, list[Any]] = {
        "status": ["active", "paused", "muted", "unsubscribed"],
        "category_id": [None, cat_a, cat_b, cat_a],
        "expiration_days_override": [None, 0, 14, 90],
        "research_policy.enabled": [False, True, False, True],
        "research_policy.fetch_pdfs": [False, True, False, True],
    }
    vals = domain_values[field]
    n = min(len(timestamps), len(choices))
    muts = [(vals[choices[i]], timestamps[i]) for i in range(n)]

    def _run(order: tuple[tuple[Any, int], ...]) -> tuple[Any, datetime]:
        cur_val = vals[0]
        cur_ts = BASE_TIME - timedelta(hours=1)
        for val, sec in order:
            eff_ts, err = clamp_client_timestamp(_offset_ts(sec), BASE_TIME)
            assert err is None and eff_ts is not None
            _, cur_val, cur_ts = merge_feed_field(
                field=field,
                current_value=cur_val,
                stored_ts=cur_ts,
                new_value=val,
                effective_ts=eff_ts,
            )
        return cur_val, cur_ts

    perms = list(itertools.islice(itertools.permutations(muts), 24))
    expected = _run(perms[0])
    for perm in perms[1:]:
        assert _run(perm) == expected


def test_clock_skew_clamp_dead_rtc_and_naive_rejection() -> None:
    # Future +1 year clamped to server_now + 5 min
    eff, err = clamp_client_timestamp(BASE_TIME + timedelta(days=365), BASE_TIME)
    assert err is None
    assert eff == BASE_TIME + timedelta(minutes=5)

    # Within +5m kept verbatim
    t_ok = BASE_TIME + timedelta(minutes=3)
    eff, err = clamp_client_timestamp(t_ok, BASE_TIME)
    assert err is None
    assert eff == t_ok

    # Legitimate 80-day offline mutation allowed
    t_offline = BASE_TIME - timedelta(days=80)
    eff, err = clamp_client_timestamp(t_offline, BASE_TIME)
    assert err is None
    assert eff == t_offline

    # Dead-RTC (> 90 days in the past) rejected as clock_skew (Q-3)
    eff, err = clamp_client_timestamp(datetime(1970, 1, 1, tzinfo=UTC), BASE_TIME)
    assert eff is None
    assert err == "clock_skew"

    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_utc(datetime(2026, 11, 25, 12, 0, 0))


def test_online_monotonic_lww_and_field_timestamp_parsing() -> None:
    # Older stored ts -> returns db_now
    assert compute_online_lww_timestamp(BASE_TIME, BASE_TIME - timedelta(seconds=10)) == BASE_TIME
    assert compute_online_lww_timestamp(BASE_TIME, None) == BASE_TIME

    # Equal or future-clamped stored ts -> advances monotonically by 1 microsecond (P-02)
    future_stored = BASE_TIME + timedelta(minutes=4)
    assert compute_online_lww_timestamp(BASE_TIME, future_stored) == future_stored + timedelta(
        microseconds=1
    )
    assert compute_online_lww_timestamp(BASE_TIME, BASE_TIME) == BASE_TIME + timedelta(
        microseconds=1
    )

    fallback = BASE_TIME - timedelta(days=1)
    # Granular key wins over parent key
    ts_map = {
        "research_policy": (BASE_TIME - timedelta(hours=2)).isoformat(),
        "research_policy.fetch_pdfs": BASE_TIME.isoformat(),
        "bad": "not-an-iso-ts",
    }
    assert parse_field_timestamp(ts_map, "research_policy.fetch_pdfs", fallback) == BASE_TIME
    # Fallback to parent research_policy key for 1a rows
    assert parse_field_timestamp(
        ts_map, "research_policy.enabled", fallback
    ) == BASE_TIME - timedelta(hours=2)
    # Invalid ISO falls back safely
    assert parse_field_timestamp(ts_map, "bad", fallback) == fallback
    assert parse_field_timestamp(None, "status", fallback) == fallback


def test_summary_capping_lead_excerpt_payload_hash_and_status_transitions() -> None:
    assert cap_raw_summary(None) == (None, False)
    exact = "a" * MAX_RAW_SUMMARY_CHARS
    assert cap_raw_summary(exact) == (exact, False)
    over = "b" * (MAX_RAW_SUMMARY_CHARS + 5)
    capped, truncated = cap_raw_summary(over)
    assert truncated is True
    assert capped is not None and len(capped) == MAX_RAW_SUMMARY_CHARS

    # lead_excerpt prefers extracted_text, else stripped HTML summary, else None
    assert derive_lead_excerpt(extracted_text="  hello\n\tworld  ", raw_summary="<p>x</p>") == (
        "hello world"
    )
    assert derive_lead_excerpt(
        extracted_text="   ",
        raw_summary="<p>Alpha</p><script>evil()</script><p>Beta &amp; Gamma</p>",
    ) == ("Alpha Beta & Gamma")
    assert derive_lead_excerpt(extracted_text=None, raw_summary=None) is None
    assert derive_lead_excerpt(extracted_text="", raw_summary="   ") is None
    long_excerpt = derive_lead_excerpt(extracted_text="x" * 3000, raw_summary=None)
    assert long_excerpt is not None and len(long_excerpt) == LEAD_EXCERPT_CHARS

    # Deterministic payload SHA-256 regardless of dict key order
    h1 = compute_payload_sha256({"b": 2, "a": 1})
    h2 = compute_payload_sha256({"a": 1, "b": 2})
    assert h1 == h2
    assert h1 != compute_payload_sha256({"a": 1, "b": 3})

    with pytest.raises(ValueError, match="Unsupported LWW feed field"):
        rank_feed_field_value("unknown_field", "x")

    # Shared status transition helper (Q-4)
    feed = SimpleNamespace(
        status="unsubscribed",
        etag='"e"',
        last_modified="Mon",
        health_state="broken",
        consecutive_errors=5,
        last_error="boom",
        initial_backfill_done=True,
        next_fetch_at=BASE_TIME + timedelta(hours=5),
    )
    assert apply_feed_status_transition(feed, "active", BASE_TIME) is True
    assert feed.status == "active"
    assert feed.etag is None
    assert feed.last_modified is None
    assert feed.health_state == "healthy"
    assert feed.consecutive_errors == 0
    assert feed.last_error is None
    assert feed.initial_backfill_done is False
    assert feed.next_fetch_at == BASE_TIME

    # paused -> active pulls future next_fetch_at forward without forcing poll job
    paused_feed = SimpleNamespace(status="paused", next_fetch_at=BASE_TIME + timedelta(hours=3))
    assert apply_feed_status_transition(paused_feed, "active", BASE_TIME) is False
    assert paused_feed.next_fetch_at == BASE_TIME

    # paused -> muted with earlier next_fetch_at keeps earlier timestamp
    past = BASE_TIME - timedelta(minutes=10)
    paused_past = SimpleNamespace(status="paused", next_fetch_at=past)
    assert apply_feed_status_transition(paused_past, "muted", BASE_TIME) is False
    assert paused_past.next_fetch_at == past

    # active -> muted returns False
    active_feed = SimpleNamespace(status="active", next_fetch_at=BASE_TIME)
    assert apply_feed_status_transition(active_feed, "muted", BASE_TIME) is False
    assert active_feed.status == "muted"
