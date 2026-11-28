"""Unit & script tests for Milestone 1d-ops (T01..T05, T13)."""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RENDER_ENV_PATH = REPO_ROOT / "scripts" / "_render_env.py"


def _load_render_env_module() -> Any:
    spec = importlib.util.spec_from_file_location("smartrss_render_env", RENDER_ENV_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_1d_ops_t01_render_env_generate_host_lists_and_atomic_no_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1d-ops-T01 (O-07, O-09, S3): mode-0600 .env generation, primary host selection,
    CIDR/host validation, and atomic os.link non-overwrite even under concurrent creation."""
    mod = _load_render_env_module()
    template = REPO_ROOT / ".env.example"

    # 1. Explicit --lan-host -> first host is primary, daemon mode sets COMPOSE_PROFILES=daemon
    env1 = tmp_path / "one.env"
    res1 = subprocess.run(
        [
            sys.executable,
            str(RENDER_ENV_PATH),
            "generate",
            "--template",
            str(template),
            "--output",
            str(env1),
            "--lan-port",
            "9080",
            "--worker-mode",
            "daemon",
            "--lan-host",
            "smartrss.lan,192.168.1.50",
            "--ssrf-blocked-cidrs",
            "203.0.113.5/32",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode == 0, res1.stderr
    meta1 = json.loads(res1.stdout)
    assert meta1["primary_lan_host"] == "smartrss.lan"
    assert meta1["lan_port"] == 9080
    assert meta1["worker_mode"] == "daemon"
    assert "*" not in meta1["cors_allow_origins"]
    assert stat.S_IMODE(env1.stat().st_mode) == 0o600

    parsed1 = mod.parse_env_file(env1)
    pw1 = parsed1["POSTGRES_PASSWORD"]
    assert len(pw1) >= 32
    assert pw1 != mod.PLACEHOLDER_PASSWORD
    assert parsed1["DATABASE_URL"] == f"postgresql+psycopg://smartrss:{pw1}@db:5432/smartrss"
    assert parsed1["COMPOSE_PROFILES"] == "daemon"
    assert parsed1["WORKER_MODE"] == "daemon"
    assert parsed1["SSRF_BLOCKED_CIDRS"] == "203.0.113.5/32"
    assert "smartrss.lan:9080" in parsed1["ALLOWED_HOSTS"]
    assert "192.168.1.50:9080" in parsed1["ALLOWED_HOSTS"]
    assert "http://smartrss.lan:9080" in parsed1["CORS_ALLOW_ORIGINS"]
    assert "http://localhost:4180" in parsed1["CORS_ALLOW_ORIGINS"]
    assert pw1 not in res1.stdout and pw1 not in res1.stderr

    # 2. Auto-detected IPv4 + mDNS .local -> IPv4 is primary, cron mode clears COMPOSE_PROFILES
    env2 = tmp_path / "two.env"
    res2 = subprocess.run(
        [
            sys.executable,
            str(RENDER_ENV_PATH),
            "generate",
            "--template",
            str(template),
            "--output",
            str(env2),
            "--worker-mode",
            "cron",
            "--detected-ipv4",
            "192.168.1.77",
            "--mdns-host",
            "homeserver.local",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode == 0, res2.stderr
    meta2 = json.loads(res2.stdout)
    assert meta2["primary_lan_host"] == "192.168.1.77"
    parsed2 = mod.parse_env_file(env2)
    assert parsed2["COMPOSE_PROFILES"] == ""
    assert "homeserver.local:8080" in parsed2["ALLOWED_HOSTS"]
    assert "http://homeserver.local:8080" in parsed2["CORS_ALLOW_ORIGINS"]

    # 3. Fallback to localhost when neither explicit host nor IPv4 detected
    primary_fb, allowed_fb, cors_fb = mod.compute_host_lists(
        lan_hosts=[], detected_ipv4=None, mdns_host=None, lan_port=8080
    )
    assert primary_fb == "localhost"
    assert "localhost:8080" in allowed_fb
    assert "http://localhost:4180" in cors_fb

    # 4. Reject invalid CIDR, invalid host, invalid port
    for bad_args in (
        ["--lan-host", "http://bad.lan:8080"],
        ["--lan-host", "bad host"],
        ["--lan-host", "bad.lan:8080"],
        ["--ssrf-blocked-cidrs", "not-a-cidr"],
        ["--lan-port", "70000"],
        ["--worker-mode", "bogus"],
    ):
        res_bad = subprocess.run(
            [
                sys.executable,
                str(RENDER_ENV_PATH),
                "generate",
                "--template",
                str(template),
                "--output",
                str(tmp_path / "never.env"),
                *bad_args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_bad.returncode == 2
        assert not (tmp_path / "never.env").exists()

    # 5. Existing file returns PRESERVED_EXIT_CODE (10) untouched
    orig_bytes = env1.read_bytes()
    res_dup = subprocess.run(
        [
            sys.executable,
            str(RENDER_ENV_PATH),
            "generate",
            "--template",
            str(template),
            "--output",
            str(env1),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_dup.returncode == mod.PRESERVED_EXIT_CODE
    assert env1.read_bytes() == orig_bytes

    # 6. O-09: Concurrent creation between pre-check and os.link() does NOT overwrite target
    raced_env = tmp_path / "raced.env"
    orig_link = os.link

    def _race_link(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        # Simulate another process creating dst right before os.link()
        Path(dst).write_text("POSTGRES_PASSWORD=winner-secret\n", encoding="utf-8")
        orig_link(src, dst)

    monkeypatch.setattr(mod.os, "link", _race_link)
    created = mod.write_env_atomically_no_overwrite(raced_env, "POSTGRES_PASSWORD=loser\n")
    assert created is False
    assert raced_env.read_text(encoding="utf-8") == "POSTGRES_PASSWORD=winner-secret\n"
    # Temp file cleaned up
    assert [p.name for p in tmp_path.glob(".raced.env.tmp.*")] == []


def test_1d_ops_t02_render_env_inspect_non_secret_and_no_shell_exec(tmp_path: Path) -> None:
    """1d-ops-T02 (S3): inspect parses .env safely without shell execution and hides secrets."""
    marker = tmp_path / "pwned"
    env_file = tmp_path / "existing.env"
    env_file.write_text(
        f"# comment\n"
        f"POSTGRES_PASSWORD=change-me-to-a-strong-password\n"
        f"DATABASE_URL=postgresql+psycopg://smartrss:top-secret-pw@db:5432/smartrss\n"
        f"LAN_PORT=8181\n"
        f"WORKER_MODE=cron\n"
        f"ALLOWED_HOSTS=localhost,127.0.0.1,192.168.50.9:8181\n"
        f"SHELL_INJECTION=$(touch {marker})\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    res = subprocess.run(
        [sys.executable, str(RENDER_ENV_PATH), "inspect", "--env-file", str(env_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not marker.exists()
    assert "top-secret-pw" not in res.stdout
    assert "change-me-to-a-strong-password" not in res.stdout
    info = json.loads(res.stdout)
    assert info["lan_port"] == 8181
    assert info["worker_mode"] == "cron"
    assert info["primary_lan_host"] == "192.168.50.9"
    assert info["insecure_permissions"] is True
    assert info["placeholder_password"] is True


def test_1d_ops_t03_setup_server_prereq_failures_never_run_sudo(tmp_path: Path) -> None:
    """1d-ops-T03: missing docker / compose / daemon access exits 1 and never invokes sudo."""
    script = str(REPO_ROOT / "scripts" / "setup-server.sh")
    sudo_log = tmp_path / "sudo.log"

    def _base_bin() -> Path:
        b = tmp_path / f"bin-{time.monotonic_ns()}"
        b.mkdir()
        _make_executable(
            b / "sudo",
            f"#!/usr/bin/env bash\necho sudo-called >> {sudo_log}\nexit 99\n",
        )
        # Provide python3 & basic coreutils symlinks so only docker is controlled
        for tool in ("bash", "python3", "awk", "grep", "wc", "cat", "dirname", "cd", "env"):
            found = subprocess.run(["which", tool], capture_output=True, text=True, check=False)
            if found.returncode == 0:
                target = Path(found.stdout.strip())
                if target.exists() and not (b / tool).exists():
                    (b / tool).symlink_to(target)
        return b

    # 1. Missing docker completely
    b1 = _base_bin()
    r1 = subprocess.run(
        [script, "--non-interactive", "--env-file", str(tmp_path / "e1.env")],
        capture_output=True,
        text=True,
        env={"PATH": str(b1), "HOME": str(tmp_path)},
        check=False,
    )
    assert r1.returncode == 1
    assert "docker" in r1.stderr.lower()
    assert not sudo_log.exists()

    # 2. docker present, docker compose missing
    b2 = _base_bin()
    _make_executable(b2 / "docker", "#!/usr/bin/env bash\nexit 1\n")
    r2 = subprocess.run(
        [script, "--non-interactive", "--env-file", str(tmp_path / "e2.env")],
        capture_output=True,
        text=True,
        env={"PATH": str(b2), "HOME": str(tmp_path)},
        check=False,
    )
    assert r2.returncode == 1
    assert "docker compose" in r2.stderr
    assert not sudo_log.exists()

    # 3. docker compose present, docker info fails (no daemon access)
    b3 = _base_bin()
    _make_executable(
        b3 / "docker",
        """#!/usr/bin/env bash
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  echo "Docker Compose version v2.30.0"
  exit 0
fi
if [[ "$1" == "volume" && "$2" == "inspect" ]]; then
  exit 1
fi
if [[ "$1" == "info" ]]; then
  exit 1
fi
exit 0
""",
    )
    r3 = subprocess.run(
        [script, "--non-interactive", "--env-file", str(tmp_path / "e3.env")],
        capture_output=True,
        text=True,
        env={"PATH": str(b3), "HOME": str(tmp_path)},
        check=False,
    )
    assert r3.returncode == 1
    assert "usermod -aG docker" in r3.stderr
    assert not sudo_log.exists()


def test_1d_ops_t04_compose_shared_image_and_setup_server_flows(tmp_path: Path) -> None:
    """1d-ops-T04 (O-01, O-01-F1, O-06, O-07, O-10): shared backend image + worker.build,
    ordered startup, idempotent re-run, cron rebuild, timer + linger/crontab warnings."""
    # (a) Compose config: api & worker share image: smartrss-backend:local AND worker has build
    compose_env = {**os.environ, "POSTGRES_PASSWORD": "test-only-password"}
    cfg_res = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(REPO_ROOT / "docker-compose.yml"),
            "--profile",
            "daemon",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        env=compose_env,
        check=True,
    )
    cfg = json.loads(cfg_res.stdout)
    api_svc = cfg["services"]["api"]
    worker_svc = cfg["services"]["worker"]
    assert api_svc["image"] == "smartrss-backend:local"
    assert worker_svc["image"] == "smartrss-backend:local"
    assert worker_svc.get("build", {}).get("context", "").endswith("/backend")

    # Local stub HTTP /api/v1/health server returning status, db, and version (O-10 subset match)
    class _HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"status": "ok", "db": "ok", "version": "0.1.0"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _HealthHandler)
    health_port = srv.server_address[1]
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()

    try:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker_log = tmp_path / "docker.log"
        opml_received = tmp_path / "opml_received.xml"
        systemctl_log = tmp_path / "systemctl.log"

        _make_executable(
            bin_dir / "docker",
            f"""#!/usr/bin/env bash
echo "$*" >> "{docker_log}"
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  echo "Docker Compose version v2.30.0"
  exit 0
fi
if [[ "$1" == "volume" && "$2" == "inspect" ]]; then
  exit 1
fi
if [[ "$1" == "info" ]]; then
  echo "Server Version: 29.0"
  exit 0
fi
if [[ "$*" == *"feeds import-opml -"* ]]; then
  cat > "{opml_received}"
  echo "imported=1 reactivated=0 skipped_duplicates=0 rejected_invalid=0"
  exit 0
fi
if [[ "$*" == *"pair create"* ]]; then
  echo "Pairing code: ABCD-EFGH-JKLM-NPQR"
  echo "Expires at:   2026-11-27T19:00:00+00:00"
  exit 0
fi
exit 0
""",
        )
        _make_executable(
            bin_dir / "systemctl",
            f'#!/usr/bin/env bash\necho "$*" >> "{systemctl_log}"\nexit 0\n',
        )
        _make_executable(
            bin_dir / "loginctl",
            "#!/usr/bin/env bash\necho no\nexit 0\n",
        )
        crontab_line = (
            "*/15 * * * * cd /srv/smartrss && flock -n ~/.local/state/smartrss/worker.lock "
            "docker compose run --rm -T worker smartrss worker run-once --stage all --json"
        )
        _make_executable(
            bin_dir / "crontab",
            f'#!/usr/bin/env bash\necho "{crontab_line}"\n',
        )

        opml_src = tmp_path / "seed.opml"
        opml_src.write_text(
            '<?xml version="1.0"?><opml version="2.0"><body>'
            '<outline text="Test" xmlUrl="https://example.com/rss"/>'
            "</body></opml>\n",
            encoding="utf-8",
        )

        env_file = tmp_path / "server.env"
        xdg_cfg = tmp_path / "xdg-cfg"
        run_env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "XDG_CONFIG_HOME": str(xdg_cfg),
            "SETUP_HEALTH_URL": f"http://127.0.0.1:{health_port}/api/v1/health",
            "SETUP_HEALTH_TIMEOUT_SECONDS": "5",
        }
        script = str(REPO_ROOT / "scripts" / "setup-server.sh")

        # (b) First run in cron mode with --install-worker-timer + --opml
        res1 = subprocess.run(
            [
                script,
                "--non-interactive",
                "--worker-mode",
                "cron",
                "--lan-host",
                "192.168.1.90",
                "--lan-port",
                "8080",
                "--install-worker-timer",
                "--opml",
                str(opml_src),
                "--env-file",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=run_env,
            check=False,
        )
        assert res1.returncode == 0, res1.stderr
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        assert "make install-client API_ORIGIN=http://192.168.1.90:8080" in res1.stdout
        assert "DHCP reservation" in res1.stdout
        assert "ABCD-EFGH-JKLM-NPQR" in res1.stdout
        assert "sudo loginctl enable-linger" in res1.stdout
        assert "Existing SmartRSS crontab entry detected" in res1.stderr
        assert opml_received.read_text(encoding="utf-8") == opml_src.read_text(encoding="utf-8")

        # Check ordered startup (O-01, O-10): build api web -> up db api -> migrate -> up
        cmds1 = docker_log.read_text(encoding="utf-8").splitlines()
        idx_build = cmds1.index("compose build api web")
        idx_up_core = cmds1.index("compose up -d --wait db api")
        idx_migrate = cmds1.index("compose exec -T api smartrss db migrate")
        idx_up_all = cmds1.index("compose up -d --wait")
        assert idx_build < idx_up_core < idx_migrate < idx_up_all

        # Check rendered systemd user unit (O-06, O-10)
        svc_unit = (xdg_cfg / "systemd" / "user" / "smartrss-worker.service").read_text(
            encoding="utf-8"
        )
        assert f"WorkingDirectory={REPO_ROOT}" in svc_unit
        assert "%h/.local/state/smartrss/worker.lock" in svc_unit
        assert "flock -n -E 75" in svc_unit
        assert "SuccessExitStatus=75" in svc_unit
        assert "network-online.target" not in svc_unit
        assert (xdg_cfg / "systemd" / "user" / "smartrss-worker.timer").is_file()
        assert "enable --now smartrss-worker.timer" in systemctl_log.read_text(encoding="utf-8")

        # (c) Second run preserves .env, runs `compose build api web`, honors --skip-pair
        env_before = env_file.read_bytes()
        docker_log.unlink()
        res2 = subprocess.run(
            [
                script,
                "--non-interactive",
                "--skip-pair",
                "--env-file",
                str(env_file),
            ],
            capture_output=True,
            text=True,
            env=run_env,
            check=False,
        )
        assert res2.returncode == 0, res2.stderr
        assert env_file.read_bytes() == env_before
        assert "Preserved existing" in res2.stdout
        cmds2 = docker_log.read_text(encoding="utf-8").splitlines()
        assert "compose build api web" in cmds2
        assert not any("pair create" in c for c in cmds2)
    finally:
        srv.shutdown()
        srv.server_close()


def test_1d_ops_t05_backup_script_opml_snapshot_rotation_and_rsync_on_failure(
    tmp_path: Path,
) -> None:
    """1d-ops-T05 (E-11, O-03): verified mode-0600 OPML snapshot alongside dump,
    exec/run fallback, pruning, rsync inclusion, and rsync resilience on OPML failure."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    backup_dir = tmp_path / "backups"
    rsync_log = tmp_path / "rsync.log"
    ssh_log = tmp_path / "ssh.log"
    docker_log = tmp_path / "docker.log"
    api_running_flag = tmp_path / "api_running"
    fail_opml_flag = tmp_path / "fail_opml"

    _make_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
echo "$*" >> "{docker_log}"
if [[ "$*" == *"pg_dump"* ]]; then
  printf 'PGDMP-stub-archive-data'
  exit 0
fi
if [[ "$*" == *"pg_restore --list"* ]]; then
  cat >/dev/null
  echo "1; 0 0 TABLE DATA public feeds"
  exit 0
fi
if [[ "$*" == *"ps --status running --services"* ]]; then
  if [[ -f "{api_running_flag}" ]]; then
    echo "db"
    echo "api"
  else
    echo "db"
  fi
  exit 0
fi
if [[ "$*" == *"feeds export-opml"* ]]; then
  if [[ -f "{fail_opml_flag}" ]]; then
    echo "simulated export failure" >&2
    exit 1
  fi
  echo '<?xml version="1.0" encoding="UTF-8"?><opml version="2.0"><body><outline text="A" xmlUrl="https://example.com/rss"/></body></opml>'
  exit 0
fi
exit 0
""",
    )
    _make_executable(
        bin_dir / "rsync",
        f'#!/usr/bin/env bash\necho "$*" >> "{rsync_log}"\nexit 0\n',
    )
    _make_executable(
        bin_dir / "ssh",
        f'#!/usr/bin/env bash\necho "$*" >> "{ssh_log}"\nexit 0\n',
    )

    script = str(REPO_ROOT / "scripts" / "backup.sh")
    base_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "BACKUP_DIR": str(backup_dir),
        "BACKUP_RSYNC_TARGET": "backup@nas.lan:/srv/smartrss",
        "BACKUP_LOCAL_RETENTION_DAYS": "14",
        "BACKUP_REMOTE_RETENTION_DAYS": "30",
    }

    # Seed stale dump & OPML older than 14 days to verify independent pruning
    backup_dir.mkdir(parents=True)
    stale_dump = backup_dir / "smartrss-20200101T000000Z.dump"
    stale_opml = backup_dir / "smartrss-feeds-20200101T000000Z.opml"
    stale_dump.write_bytes(b"old-dump")
    stale_opml.write_bytes(b"<opml></opml>")
    old_ts = time.time() - 20 * 86400
    os.utime(stale_dump, (old_ts, old_ts))
    os.utime(stale_opml, (old_ts, old_ts))

    # (a, b, c) API running -> exec path creates 0600 dump + 0600 opml, prunes stale, rsyncs both
    api_running_flag.write_text("1", encoding="utf-8")
    r1 = subprocess.run(
        [script],
        cwd=tmp_path,  # isolate from repo .env
        capture_output=True,
        text=True,
        env=base_env,
        check=False,
    )
    assert r1.returncode == 0, r1.stderr
    assert not stale_dump.exists()
    assert not stale_opml.exists()
    dumps = list(backup_dir.glob("smartrss-*.dump"))
    opmls = list(backup_dir.glob("smartrss-feeds-*.opml"))
    assert len(dumps) == 1 and len(opmls) == 1
    assert stat.S_IMODE(dumps[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(opmls[0].stat().st_mode) == 0o600
    assert "<opml" in opmls[0].read_text(encoding="utf-8")
    assert "exec -T api smartrss feeds export-opml" in docker_log.read_text(encoding="utf-8")

    rsync_lines = rsync_log.read_text(encoding="utf-8")
    assert "--delete" not in rsync_lines
    assert dumps[0].name in rsync_lines
    assert opmls[0].name in rsync_lines
    ssh_lines = ssh_log.read_text(encoding="utf-8")
    assert "smartrss-*.dump" in ssh_lines
    assert "smartrss-feeds-*.opml" in ssh_lines

    # (a2) API stopped -> falls back to `docker compose run --rm -T --no-deps api ...`
    api_running_flag.unlink()
    docker_log.unlink()
    r2 = subprocess.run(
        [script], cwd=tmp_path, capture_output=True, text=True, env=base_env, check=False
    )
    assert r2.returncode == 0, r2.stderr
    assert "run --rm -T --no-deps api smartrss feeds export-opml" in docker_log.read_text(
        encoding="utf-8"
    )

    # (d) O-03: OPML export fails with BACKUP_RSYNC_TARGET set -> rsync STILL called with new dump,
    # no .opml / .opml.tmp left for this run, and backup.sh exits 1
    for p in backup_dir.iterdir():
        p.unlink()
    rsync_log.unlink()
    fail_opml_flag.write_text("1", encoding="utf-8")
    r3 = subprocess.run(
        [script], cwd=tmp_path, capture_output=True, text=True, env=base_env, check=False
    )
    assert r3.returncode == 1
    assert "OPML snapshot export failed" in r3.stderr
    dumps3 = list(backup_dir.glob("smartrss-*.dump"))
    assert len(dumps3) == 1
    assert list(backup_dir.glob("smartrss-feeds-*.opml*")) == []
    assert rsync_log.is_file()
    assert dumps3[0].name in rsync_log.read_text(encoding="utf-8")


def test_1d_ops_t13_make_install_client_api_origin_alias() -> None:
    """1d-ops-T13: make install-client API_ORIGIN=... forwards --api-origin cleanly."""
    res = subprocess.run(
        ["make", "-n", "install-client", "API_ORIGIN=http://192.168.1.50:8080"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert '--api-origin "http://192.168.1.50:8080"' in res.stdout


def test_g01_setup_existing_env_validates_flags_warns_conflicts_and_cron_summary(
    tmp_path: Path,
) -> None:
    """G-01 regression: with an existing .env, setup-server.sh still validates host/CIDR flags
    before Docker, flags conflicting --worker-mode/--install-worker-timer/--lan-host/--lan-port/
    --ssrf-blocked-cidrs (exit 2 under --non-interactive), and warns when cron mode has no
    scheduler configured."""

    class _HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"status": "ok", "db": "ok", "version": "0.1.0"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _HealthHandler)
    health_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker_log = tmp_path / "docker.log"
        _make_executable(
            bin_dir / "docker",
            f"""#!/usr/bin/env bash
echo "$*" >> "{docker_log}"
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  echo "Docker Compose version v2.30.0"
  exit 0
fi
if [[ "$1" == "volume" && "$2" == "inspect" ]]; then
  exit 1
fi
exit 0
""",
        )
        _make_executable(bin_dir / "crontab", "#!/usr/bin/env bash\nexit 1\n")

        script = str(REPO_ROOT / "scripts" / "setup-server.sh")
        run_env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "SETUP_HEALTH_URL": f"http://127.0.0.1:{health_port}/api/v1/health",
            "SETUP_HEALTH_TIMEOUT_SECONDS": "5",
        }

        daemon_env = tmp_path / "daemon.env"
        daemon_env.write_text(
            "POSTGRES_PASSWORD=strong-secret\n"
            "LAN_PORT=8080\n"
            "ALLOWED_HOSTS=localhost,192.168.1.50,192.168.1.50:8080\n"
            "CORS_ALLOW_ORIGINS=http://localhost:8080,http://localhost:4180\n"
            "SSRF_BLOCKED_CIDRS=\n"
            "WORKER_MODE=daemon\n"
            "COMPOSE_PROFILES=daemon\n",
            encoding="utf-8",
        )
        os.chmod(daemon_env, 0o600)

        # (a) Invalid CIDR and invalid host with existing .env -> exit 2 BEFORE any docker call
        r_bad_cidr = subprocess.run(
            [
                script,
                "--non-interactive",
                "--env-file",
                str(daemon_env),
                "--ssrf-blocked-cidrs",
                "not-a-cidr",
                "--skip-pair",
            ],
            capture_output=True,
            text=True,
            env=run_env,
            check=False,
        )
        assert r_bad_cidr.returncode == 2
        assert "Invalid CIDR" in r_bad_cidr.stderr
        assert not docker_log.exists()

        r_bad_host = subprocess.run(
            [
                script,
                "--non-interactive",
                "--env-file",
                str(daemon_env),
                "--lan-host",
                "http://bad host:9",
                "--skip-pair",
            ],
            capture_output=True,
            text=True,
            env=run_env,
            check=False,
        )
        assert r_bad_host.returncode == 2
        assert "Invalid LAN host" in r_bad_host.stderr
        assert not docker_log.exists()

        # (b) Conflicting flags against preserved daemon .env under --non-interactive -> exit 2
        # with explicit conflict warnings and NO docker compose build/up calls
        r_conflict = subprocess.run(
            [
                script,
                "--non-interactive",
                "--env-file",
                str(daemon_env),
                "--worker-mode",
                "cron",
                "--install-worker-timer",
                "--lan-port",
                "9090",
                "--lan-host",
                "192.168.1.99",
                "--ssrf-blocked-cidrs",
                "203.0.113.5/32",
                "--skip-pair",
            ],
            capture_output=True,
            text=True,
            env=run_env,
            check=False,
        )
        assert r_conflict.returncode == 2
        assert "--worker-mode cron ignored" in r_conflict.stderr
        assert "--install-worker-timer ignored" in r_conflict.stderr
        assert "--lan-port 9090 ignored" in r_conflict.stderr
        assert "--lan-host 192.168.1.99 ignored" in r_conflict.stderr
        assert "--ssrf-blocked-cidrs" in r_conflict.stderr
        cmds_conflict = docker_log.read_text(encoding="utf-8") if docker_log.exists() else ""
        assert "compose build" not in cmds_conflict
        assert "compose up" not in cmds_conflict

        # (c) Cron mode with --non-interactive, no timer installed, empty crontab -> warns clearly
        cron_env = tmp_path / "cron.env"
        docker_log.unlink(missing_ok=True)
        r_cron_unsched = subprocess.run(
            [
                script,
                "--non-interactive",
                "--worker-mode",
                "cron",
                "--lan-host",
                "192.168.1.60",
                "--env-file",
                str(cron_env),
                "--skip-pair",
            ],
            capture_output=True,
            text=True,
            env=run_env,
            check=False,
        )
        assert r_cron_unsched.returncode == 0, r_cron_unsched.stderr
        assert "Worker is NOT scheduled" in r_cron_unsched.stdout
    finally:
        srv.shutdown()
        srv.server_close()


def test_g02_setup_refuses_new_password_over_existing_pgdata_volume(tmp_path: Path) -> None:
    """G-02 regression: when .env is absent but the pgdata Docker volume already exists,
    setup-server.sh refuses with exit 1, creates no .env, skips compose build/up, and prints
    recovery options."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    _make_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
echo "$*" >> "{docker_log}"
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  echo "Docker Compose version v2.30.0"
  exit 0
fi
if [[ "$1" == "compose" && "$2" == "config" ]]; then
  echo '{{"name":"smartrss","volumes":{{"pgdata":{{"name":"smartrss_pgdata"}}}}}}'
  exit 0
fi
if [[ "$1" == "volume" && "$2" == "inspect" && "$3" == "smartrss_pgdata" ]]; then
  echo '[{{"Name":"smartrss_pgdata"}}]'
  exit 0
fi
exit 0
""",
    )

    env_file = tmp_path / "missing.env"
    res = subprocess.run(
        [
            str(REPO_ROOT / "scripts" / "setup-server.sh"),
            "--non-interactive",
            "--env-file",
            str(env_file),
            "--skip-pair",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
        check=False,
    )
    assert res.returncode == 1
    assert not env_file.exists()
    assert "smartrss_pgdata" in res.stderr
    assert "Restore your previous .env" in res.stderr
    assert "backups/" in res.stderr
    assert "docker volume rm smartrss_pgdata" in res.stderr
    cmds = docker_log.read_text(encoding="utf-8")
    assert "compose build" not in cmds
    assert "compose up" not in cmds
