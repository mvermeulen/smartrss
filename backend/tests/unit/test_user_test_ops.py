"""Unit and ops regression tests for 1a–1c user-test findings (E-02, E-03, E-04, E-13)."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from smartrss.logging import HealthCheckAccessFilter, configure_logging

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_e02_python_version_pin_present() -> None:
    pin_file = REPO_ROOT / "backend" / ".python-version"
    assert pin_file.is_file()
    assert pin_file.read_text(encoding="utf-8").strip() == "3.12"

    pyproject = (REPO_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.12,<3.14"' in pyproject


def test_e03_check_prereqs_rejects_inaccessible_docker_daemon(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    flag_file = tmp_path / "daemon_ok"

    _make_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  echo "Docker Compose version v2.29.0"
  exit 0
fi
if [[ "$1" == "info" ]]; then
  if [[ -f "{flag_file}" ]]; then
    echo "Server Version: 29.0"
    exit 0
  fi
  echo "permission denied while trying to connect to the Docker daemon socket" >&2
  exit 1
fi
exit 0
""",
    )

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    script = str(REPO_ROOT / "scripts" / "check-prereqs.sh")

    # Without daemon access -> fails with actionable hint
    res_fail = subprocess.run(
        [script, "--mode", "server"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res_fail.returncode == 1
    assert "docker info" in res_fail.stderr
    assert "usermod -aG docker" in res_fail.stderr

    # With daemon access -> succeeds
    flag_file.write_text("ok", encoding="utf-8")
    res_ok = subprocess.run(
        [script, "--mode", "server"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res_ok.returncode == 0, res_ok.stderr


def test_e04_install_client_api_origin_flag(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_executable(bin_dir / "systemctl", "#!/usr/bin/env bash\nexit 0\n")

    dist_dir = tmp_path / "prebuilt-dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>ok</html>\n", encoding="utf-8")

    xdg_data = tmp_path / "xdg-data"
    xdg_config = tmp_path / "xdg-config"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_CONFIG_HOME": str(xdg_config),
    }
    script = str(REPO_ROOT / "scripts" / "install-client.sh")

    # 1. Fresh install with --api-origin writes normalized origin and preserves port
    res1 = subprocess.run(
        [script, "--from-dist", str(dist_dir), "--api-origin", "http://192.168.1.50:8080/"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res1.returncode == 0, res1.stderr
    cfg_file = xdg_config / "smartrss-client" / "config.env"
    assert cfg_file.is_file()
    cfg_text = cfg_file.read_text(encoding="utf-8")
    assert "SMARTRSS_API_ORIGIN=http://192.168.1.50:8080" in cfg_text
    assert "SMARTRSS_CLIENT_PORT=4180" in cfg_text
    assert (xdg_data / "smartrss" / "dist" / "index.html").is_file()

    # 2. Re-run with --api-origin=... updates existing config.env in place
    res2 = subprocess.run(
        [script, "--from-dist", str(dist_dir), "--api-origin=http://smartrss.lan:8080"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res2.returncode == 0, res2.stderr
    cfg_text2 = cfg_file.read_text(encoding="utf-8")
    assert "SMARTRSS_API_ORIGIN=http://smartrss.lan:8080" in cfg_text2
    assert "SMARTRSS_API_ORIGIN=http://192.168.1.50:8080" not in cfg_text2
    assert "SMARTRSS_CLIENT_PORT=4180" in cfg_text2

    # 3. Invalid --api-origin exits 2 without clobbering existing config.env
    res_bad = subprocess.run(
        [script, "--from-dist", str(dist_dir), "--api-origin", "javascript:alert(1)"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res_bad.returncode == 2
    assert "SMARTRSS_API_ORIGIN=http://smartrss.lan:8080" in cfg_file.read_text(encoding="utf-8")


def test_e13_healthcheck_access_log_filter() -> None:
    flt = HealthCheckAccessFilter()

    # Uvicorn 5-tuple args format for GET /api/v1/health -> dropped
    health_rec = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:43210", "GET", "/api/v1/health", "1.1", 200),
        exc_info=None,
    )
    assert flt.filter(health_rec) is False

    # Pre-formatted message format for GET /api/v1/health -> dropped
    preformatted_health = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='127.0.0.1:43210 - "GET /api/v1/health HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    assert flt.filter(preformatted_health) is False

    # Real sync/pull, system/health, and non-GET requests -> kept
    pull_rec = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("192.168.1.20:51000", "GET", "/api/v1/sync/pull?since_seq=10", "1.1", 200),
        exc_info=None,
    )
    assert flt.filter(pull_rec) is True

    sys_health_rec = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("192.168.1.20:51000", "GET", "/api/v1/system/health", "1.1", 200),
        exc_info=None,
    )
    assert flt.filter(sys_health_rec) is True

    # configure_logging attaches the filter idempotently to uvicorn.access
    configure_logging("INFO")
    configure_logging("INFO")
    access_logger = logging.getLogger("uvicorn.access")
    matching = [f for f in access_logger.filters if isinstance(f, HealthCheckAccessFilter)]
    assert len(matching) == 1


def test_e13_compose_services_have_bounded_logging() -> None:
    env = {**os.environ, "POSTGRES_PASSWORD": "test-only-password"}
    res = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(REPO_ROOT / "docker-compose.yml"),
            "--profile",
            "daemon",
            "--profile",
            "test",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    cfg = json.loads(res.stdout)
    services = cfg["services"]
    assert set(services.keys()) >= {"db", "api", "worker", "web", "db-test"}
    for name, svc in services.items():
        logging_cfg = svc.get("logging")
        assert logging_cfg is not None, f"Service {name!r} missing logging block"
        assert logging_cfg.get("driver") == "json-file", f"Service {name!r} wrong logging driver"
        opts = logging_cfg.get("options") or {}
        assert opts.get("max-size"), f"Service {name!r} missing logging.options.max-size"
        assert opts.get("max-file"), f"Service {name!r} missing logging.options.max-file"


def test_e12_compute_updated_canonical_url_fallbacks() -> None:
    from smartrss.db.recanonicalize import compute_updated_canonical_url

    # Invalid raw_url falls back to current_canonical_url
    assert (
        compute_updated_canonical_url(
            "not-a-url",
            "https://www.bbc.com/news/articles/abc?at_medium=RSS&at_campaign=rss",
        )
        == "https://www.bbc.com/news/articles/abc"
    )
    # Both invalid -> returns None safely
    assert compute_updated_canonical_url("not-a-url", "also-not-a-url") is None


async def test_e07_no_store_middleware_preserves_existing_header_and_non_http_scope() -> None:
    from smartrss.security.middleware import NoStoreResponseHeaderMiddleware

    seen_non_http: list[str] = []

    async def _non_http_app(scope: Any, _receive: Any, _send: Any) -> None:
        seen_non_http.append(str(scope["type"]))

    mw_non_http = NoStoreResponseHeaderMiddleware(_non_http_app)
    await mw_non_http({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen_non_http == ["lifespan"]

    sent: list[dict[str, Any]] = []

    async def _existing_cc_app(_scope: Any, _receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"cache-control", b"private, max-age=60")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def _capture(msg: Any) -> None:
        sent.append(dict(msg))

    mw = NoStoreResponseHeaderMiddleware(_existing_cc_app)
    await mw({"type": "http"}, None, _capture)  # type: ignore[arg-type]
    assert sent[0]["headers"] == [(b"cache-control", b"private, max-age=60")]


def test_e06_e12_cli_text_summary_and_recanonicalize(monkeypatch: Any) -> None:
    from typer.testing import CliRunner

    import smartrss.cli as cli_mod
    from smartrss.worker.runner import RunOnceSummary

    runner = CliRunner()

    class _DummyEngine:
        async def dispose(self) -> None:
            pass

    monkeypatch.setattr(cli_mod, "build_engine", lambda *a, **kw: _DummyEngine())
    monkeypatch.setattr(cli_mod, "build_sessionmaker", lambda *a, **kw: object())

    async def _fake_recanon(*_a: Any, **_kw: Any) -> int:
        return 7

    monkeypatch.setattr(cli_mod, "recanonicalize_all_items", _fake_recanon)
    res_recanon = runner.invoke(cli_mod.app, ["db", "recanonicalize", "--batch-size", "100"])
    assert res_recanon.exit_code == 0, res_recanon.output
    assert "Recanonicalized 7 items." in res_recanon.output

    async def _fake_run_once(*_a: Any, **_kw: Any) -> RunOnceSummary:
        return RunOnceSummary(
            status="completed",
            exit_code=0,
            duration_ms=42,
            feeds_polled=3,
            feeds_not_modified=2,
            items_ingested=5,
            items_polled=5,
            items_extracted=4,
            jobs_failed=0,
            jobs_deferred=0,
        )

    monkeypatch.setattr(cli_mod, "run_worker_once", _fake_run_once)
    res_text = runner.invoke(cli_mod.app, ["worker", "run-once"])
    assert res_text.exit_code == 0, res_text.output
    assert "feeds_polled=3" in res_text.output
    assert "not_modified=2" in res_text.output
    assert "ingested=5" in res_text.output
