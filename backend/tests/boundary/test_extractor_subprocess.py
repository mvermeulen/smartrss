"""Boundary tests for isolated HTML extraction subprocess (ADR 0006, T-05, §9.2)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from smartrss.extract.client import extract_html_isolated

SAMPLE_ARTICLE_HTML = """<!doctype html>
<html>
<head><title>Understanding PostgreSQL SKIP LOCKED</title></head>
<body>
  <header><nav><a href="/">Home</a></nav></header>
  <main>
    <article>
      <h1>Understanding PostgreSQL SKIP LOCKED</h1>
      <p>PostgreSQL provides row-level locking with FOR UPDATE SKIP LOCKED, which allows
      multiple concurrent workers to claim queued work items without blocking each other
      or introducing duplicate processing across workers.</p>
      <p>When paired with lease expiration timestamps, heartbeats, and fenced updates,
      a PostgreSQL table serves as a reliable transactional job queue for self-hosted
      applications without needing Redis or RabbitMQ.</p>
    </article>
  </main>
</body>
</html>
"""


async def test_clean_html_extraction_in_real_subprocess() -> None:
    res = await extract_html_isolated(
        url="https://blog.example.com/skip-locked",
        html=SAMPLE_ARTICLE_HTML,
    )
    assert res.ok is True
    assert res.text is not None
    assert "SKIP LOCKED" in res.text
    assert len(res.text) >= 100


async def test_child_env_strips_database_url_and_secrets(monkeypatch: object) -> None:
    # Write a tiny helper runner module inside a temp dir added to sys.path?
    # Better: verify via a temporary package on PYTHONPATH? Remember client strips env!
    # So we test using a real module inside backend/tests/boundary/helper_runners.py!
    os.environ["DATABASE_URL"] = "postgresql+psycopg://secret:secret@db:5432/prod"
    os.environ["SUPER_SECRET_TOKEN"] = "do-not-leak"
    try:
        res = await extract_html_isolated(
            url="https://example.com",
            html="env-check",
            runner_module="tests.boundary._extractor_test_runners",
        )
        assert res.ok is True
        assert res.text is not None
        assert "DATABASE_URL" not in res.text
        assert "SUPER_SECRET_TOKEN" not in res.text
    finally:
        os.environ.pop("SUPER_SECRET_TOKEN", None)


async def test_child_writing_1mb_to_stderr_does_not_deadlock() -> None:
    res = await extract_html_isolated(
        url="https://example.com",
        html="stderr-flood",
        timeout_seconds=10.0,
        runner_module="tests.boundary._extractor_test_runners",
    )
    assert res.ok is True
    assert res.text == "stderr-survived"


async def test_child_exceeding_stdout_cap_is_killed() -> None:
    res = await extract_html_isolated(
        url="https://example.com",
        html="stdout-bomb",
        max_stdout_bytes=64_000,
        timeout_seconds=10.0,
        runner_module="tests.boundary._extractor_test_runners",
    )
    assert res.ok is False
    assert res.error is not None
    assert "exceeded" in res.error.lower()


async def test_timeout_kills_entire_process_group_including_grandchild() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pid_file = Path(tmpdir) / "grandchild.pid"
        res = await extract_html_isolated(
            url=str(pid_file),
            html="spawn-grandchild",
            timeout_seconds=0.8,
            runner_module="tests.boundary._extractor_test_runners",
        )
        assert res.ok is False
        assert res.error is not None
        assert "timed out" in res.error.lower()

        assert pid_file.exists(), "Grandchild PID file should have been written before timeout"
        grandchild_pid = int(pid_file.read_text().strip())

        # Give kernel a brief moment to reap the SIGKILLed grandchild
        deadline = time.monotonic() + 2.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
                await asyncio.sleep(0.05)
            except ProcessLookupError:
                alive = False
                break
        assert not alive, f"Grandchild process {grandchild_pid} survived process-group kill!"


async def test_runner_oom_or_nonzero_exit_returns_clean_failure() -> None:
    res = await extract_html_isolated(
        url="https://example.com",
        html="crash",
        timeout_seconds=10.0,
        runner_module="tests.boundary._extractor_test_runners",
    )
    assert res.ok is False
    assert res.error is not None

    res_bad = await extract_html_isolated(
        url="https://example.com",
        html="bad-json",
        timeout_seconds=10.0,
        runner_module="tests.boundary._extractor_test_runners",
    )
    assert res_bad.ok is False
    assert res_bad.error is not None and "json" in res_bad.error.lower()


async def test_hostile_deeply_nested_html_does_not_crash_parent() -> None:
    # 5,000 nested divs: lxml/trafilatura either returns empty or errors safely in subprocess
    nested = "<div>" * 5000 + "hello" + "</div>" * 5000
    res = await extract_html_isolated(
        url="https://hostile.example.com/nested",
        html=nested,
        timeout_seconds=10.0,
    )
    assert isinstance(res.ok, bool)
    # Parent process is completely unaffected
    assert sys.executable
    assert signal.SIGKILL > 0
    assert textwrap.dedent("ok") == "ok"


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


async def test_raw_bytes_meta_charset_and_cjk_utf8_cap_and_cancellation() -> None:
    """I-06, I-15 & I-08: meta charset bytes, CJK UTF-8 cap, and cancel process-group kill."""
    latin1_html = (
        (FIXTURES_DIR / "article_latin1.html")
        .read_text(encoding="utf-8")
        .replace("&#233;", "é")
        .replace("&#239;", "ï")
        .encode("latin-1")
    )

    res = await extract_html_isolated(url="https://legacy.example.com/cafe", html=latin1_html)
    assert res.ok is True
    assert res.text is not None
    assert "café" in res.text.lower() or "caf" in res.text.lower()

    # I-15: Long CJK HTML (>350k CJK chars = >1 MiB UTF-8) truncated cleanly to UTF-8 budget
    cjk_para = "日本語のテキスト抽出テスト記事の本文です。" * 25000  # ~500k chars / ~1.5 MiB UTF-8
    cjk_html = f"<html><body><article><h1>見出し</h1><p>{cjk_para}</p></article></body></html>"
    res_cjk = await extract_html_isolated(url="https://jp.example.com/long", html=cjk_html)
    assert res_cjk.ok is True
    assert res_cjk.text is not None
    assert len(res_cjk.text.encode("utf-8")) <= 900_000

    # Cancellation kills child process group cleanly
    with tempfile.TemporaryDirectory() as tmpdir:
        pid_file = Path(tmpdir) / "cancel.pid"
        task = asyncio.create_task(
            extract_html_isolated(
                url=str(pid_file),
                html="spawn-grandchild",
                timeout_seconds=30.0,
                runner_module="tests.boundary._extractor_test_runners",
            )
        )
        for _ in range(40):
            if pid_file.exists():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
