"""Unit & HTTP tests for SmartRSS localhost client runner (1c-T01)."""

from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

# Import runner module directly (zero external deps)
RUNNER_DIR = Path(__file__).resolve().parent.parent
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))

import smartrss_client_runner as runner  # noqa: E402


@pytest.fixture()
def sample_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body>SmartRSS</body></html>", "utf-8")
    (dist / "sw.js").write_text("self.addEventListener('fetch', () => {});", "utf-8")
    (dist / "manifest.webmanifest").write_text('{"name":"SmartRSS"}', "utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log('ok');", "utf-8")
    (dist / "assets" / "app-abc123.css").write_text("body{margin:0;}", "utf-8")

    # Outside file + escaping symlink for traversal tests
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP_SECRET", "utf-8")
    (dist / "escape.txt").symlink_to(secret)
    return dist


@pytest.fixture()
def live_server(sample_dist: Path):
    srv = runner.SmartRSSClientServer(
        ("127.0.0.1", 0),
        dist_dir=sample_dist,
        api_origin="http://smartrss.lan:8080",
    )
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _request(
    srv: runner.SmartRSSClientServer,
    method: str,
    path: str,
    *,
    host: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.effective_port, timeout=3)
    try:
        headers = {"Host": host if host is not None else srv.canonical_host}
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, body
    finally:
        conn.close()


def test_validate_api_origin_accepts_clean_origins() -> None:
    assert runner.validate_api_origin("http://localhost:8080") == "http://localhost:8080"
    assert runner.validate_api_origin("HTTP://SmartRSS.LAN:8080/") == "http://smartrss.lan:8080"
    assert runner.validate_api_origin("https://192.168.1.50") == "https://192.168.1.50"
    assert runner.validate_api_origin("http://[::1]:8080") == "http://[::1]:8080"


@pytest.mark.parametrize(
    "bad_origin",
    [
        "",
        "   ",
        "ftp://smartrss.lan:8080",
        "javascript:alert(1)",
        "http://smartrss.lan:8080/api",
        "http://smartrss.lan:8080?x=1",
        "http://smartrss.lan:8080#frag",
        "http://user:pass@smartrss.lan:8080",
        "http://smartrss.lan:8080; script-src 'unsafe-inline'",
        "http://smartrss.lan:8080\r\nX-Evil: 1",
        "http://smartrss.lan:8080 http://evil.test",
        "http://",
        "http://smartrss.lan:99999",
    ],
)
def test_validate_api_origin_rejects_csp_injection_and_invalid(bad_origin: str) -> None:
    with pytest.raises(ValueError):
        runner.validate_api_origin(bad_origin)


def test_validate_port_bounds() -> None:
    assert runner.validate_port("4180") == 4180
    assert runner.validate_port(1024) == 1024
    assert runner.validate_port(65535) == 65535
    for bad in ("80", "1023", "65536", "abc", -1):
        with pytest.raises(ValueError):
            runner.validate_port(bad)


def test_host_allowlist_and_canonical_308_redirects(
    live_server: runner.SmartRSSClientServer,
) -> None:
    port = live_server.effective_port

    # Foreign Host rejected with 400
    for evil_host in ("evil.example", f"evil.example:{port}", "localhost:9999", ""):
        status, _hdrs, _body = _request(live_server, "GET", "/", host=evil_host)
        assert status == 400

    # 127.0.0.1:<port> and [::1]:<port> canonicalize via 308 to http://localhost:<port>
    for alt_host in (f"127.0.0.1:{port}", f"[::1]:{port}"):
        status, hdrs, body = _request(live_server, "GET", "/health?window=7d", host=alt_host)
        assert status == 308
        assert hdrs["location"] == f"http://localhost:{port}/health?window=7d"
        assert body == b""

    # Protocol-relative path normalized so 308 never becomes an open redirect
    status, hdrs, _body = _request(
        live_server, "GET", "//evil.example/steal", host=f"127.0.0.1:{port}"
    )
    assert status == 308
    assert hdrs["location"] == f"http://localhost:{port}/evil.example/steal"


def test_client_config_csp_and_cache_headers(live_server: runner.SmartRSSClientServer) -> None:
    status, hdrs, body = _request(live_server, "GET", "/client-config.json")
    assert status == 200
    assert json.loads(body) == {"mode": "localhost", "api_origin": "http://smartrss.lan:8080"}
    assert hdrs["cache-control"] == "no-store"
    assert "connect-src 'self' http://smartrss.lan:8080;" in hdrs["content-security-policy"]
    assert "img-src 'self';" in hdrs["content-security-policy"]
    assert "script-src 'self';" in hdrs["content-security-policy"]
    assert "style-src 'self';" in hdrs["content-security-policy"]
    assert hdrs["x-content-type-options"] == "nosniff"
    assert hdrs["referrer-policy"] == "no-referrer"
    assert hdrs["x-frame-options"] == "DENY"

    # Hashed assets get immutable cache control
    status, hdrs, body = _request(live_server, "GET", "/assets/app-abc123.js")
    assert status == 200
    assert hdrs["cache-control"] == "public, max-age=31536000, immutable"
    assert "javascript" in hdrs["content-type"]
    assert b"console.log('ok')" in body

    # index.html & manifest get no-cache
    status, hdrs, _body = _request(live_server, "GET", "/")
    assert status == 200
    assert hdrs["cache-control"] == "no-cache"

    # HEAD returns headers + content-length with empty body
    status, hdrs, body = _request(live_server, "HEAD", "/assets/app-abc123.css")
    assert status == 200
    assert int(hdrs["content-length"]) > 0
    assert body == b""

    # Non-GET/HEAD methods return 405
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        status, hdrs, _body = _request(live_server, method, "/")
        assert status == 405
        assert hdrs["allow"] == "GET, HEAD"


def test_sw_js_origin_tag_changes_with_api_origin(sample_dist: Path) -> None:
    srv1 = runner.SmartRSSClientServer(
        ("127.0.0.1", 0), dist_dir=sample_dist, api_origin="http://host-a.lan:8080"
    )
    srv2 = runner.SmartRSSClientServer(
        ("127.0.0.1", 0), dist_dir=sample_dist, api_origin="http://host-b.lan:8080"
    )
    t1 = threading.Thread(target=srv1.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t2 = threading.Thread(target=srv2.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t1.start()
    t2.start()
    try:
        s1, h1, b1 = _request(srv1, "GET", "/sw.js")
        s2, h2, b2 = _request(srv2, "GET", "/sw.js")
        assert s1 == 200 and s2 == 200
        assert h1["cache-control"] == "no-cache"
        assert h2["cache-control"] == "no-cache"
        assert b"// smartrss-origin-tag:" in b1
        assert b"// smartrss-origin-tag:" in b2
        assert b1 != b2
    finally:
        srv1.shutdown()
        srv2.shutdown()
        srv1.server_close()
        srv2.server_close()
        t1.join(timeout=2)
        t2.join(timeout=2)


@pytest.mark.parametrize(
    "evil_path",
    [
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2e%2e%2fsecret.txt",
        "/%252e%252e%252fsecret.txt",
        "/escape.txt",
        "/assets\\app-abc123.js",
        "/index.html%00.txt",
    ],
)
def test_path_traversal_and_symlink_escape_blocked(
    live_server: runner.SmartRSSClientServer, evil_path: str
) -> None:
    status, _hdrs, body = _request(live_server, "GET", evil_path)
    assert status in (403, 404)
    assert b"TOP_SECRET" not in body


def test_extension_aware_spa_fallback(live_server: runner.SmartRSSClientServer) -> None:
    # SPA route without extension falls back to index.html
    status, hdrs, body = _request(live_server, "GET", "/health/metrics")
    assert status == 200
    assert "text/html" in hdrs["content-type"]
    assert b"SmartRSS" in body

    # Missing asset WITH extension returns 404 (never HTML as JS)
    for missing_asset in ("/assets/missing-chunk.js", "/missing.css", "/assets/foo.map"):
        status, hdrs, body = _request(live_server, "GET", missing_asset)
        assert status == 404
        assert b"SmartRSS" not in body


def test_cli_rejects_invalid_config(tmp_path: Path) -> None:
    # Missing dist dir -> exit 2
    assert runner.main(["--dist-dir", str(tmp_path / "nope")]) == 2
    # Bad origin -> exit 2
    assert runner.main(["--dist-dir", str(tmp_path), "--api-origin", "bad://x;evil"]) == 2
    # Bad port -> exit 2
    assert runner.main(["--dist-dir", str(tmp_path), "--port", "80"]) == 2


def test_v07_runner_sigterm_graceful_shutdown(sample_dist: Path) -> None:
    """L2: runner handles SIGTERM cleanly, exits 0 within 2s, and releases its bound port."""
    import os
    import signal
    import socket
    import subprocess
    import time

    # Pick an ephemeral free port on 127.0.0.1
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    script = Path(__file__).resolve().parents[1] / "smartrss_client_runner.py"
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(script),
            "--port",
            str(port),
            "--dist-dir",
            str(sample_dist),
            "--api-origin",
            "http://192.168.1.50:8080",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        # Wait for runner to accept requests
        deadline = time.monotonic() + 5.0
        ready = False
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                try:
                    conn.request(
                        "GET", "/client-config.json", headers={"Host": f"localhost:{port}"}
                    )
                    resp = conn.getresponse()
                    resp.read()
                    if resp.status == 200:
                        ready = True
                        break
                finally:
                    conn.close()
            except OSError:
                time.sleep(0.05)
        assert ready, "Runner did not become ready in time"

        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=2.0)
        elapsed = time.monotonic() - t0
        assert rc == 0
        assert elapsed < 2.0

        # Confirm the TCP port is released and immediately bindable
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check_sock:
            check_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check_sock.bind(("127.0.0.1", port))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)


def test_v07_runner_main_signal_shutdown_inprocess(
    sample_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process coverage of main() signal handler, serve_forever, and server_close."""
    import signal

    registered: dict[int, object] = {}

    def fake_signal(sig: int, handler: object) -> None:
        registered[sig] = handler

    monkeypatch.setattr(signal, "signal", fake_signal)

    orig_serve = runner.SmartRSSClientServer.serve_forever
    missing_sigterm = False

    def serve_then_trigger_sigterm(
        self: runner.SmartRSSClientServer, *args: object, **kwargs: object
    ) -> None:
        def trigger() -> None:
            nonlocal missing_sigterm
            handler = registered.get(signal.SIGTERM)
            if callable(handler):
                handler(signal.SIGTERM, None)
            else:
                # V-07-F1: never hang serve_forever() if SIGTERM registration regresses!
                missing_sigterm = True
                self.shutdown()

        threading.Timer(0.05, trigger).start()
        orig_serve(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner.SmartRSSClientServer, "serve_forever", serve_then_trigger_sigterm)
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    rc = runner.main(
        [
            "--port",
            str(free_port),
            "--dist-dir",
            str(sample_dist),
            "--api-origin",
            "http://192.168.1.50:8080",
        ]
    )
    assert not missing_sigterm, "SIGTERM handler was not registered before serve_forever()"
    assert rc == 0
    assert signal.SIGTERM in registered
    assert signal.SIGINT in registered


def test_v09_install_client_from_docker_outside_smartrss_dir(tmp_path: Path) -> None:
    """V-09: install-client.sh --from-docker works regardless of checkout directory name."""
    import os
    import shutil
    import stat
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]
    alt_checkout = tmp_path / "smartrss-custom-checkout"
    (alt_checkout / "scripts").mkdir(parents=True)
    (alt_checkout / "client-runner").mkdir(parents=True)
    (alt_checkout / "deploy" / "systemd").mkdir(parents=True)
    (alt_checkout / "deploy" / "desktop").mkdir(parents=True)

    shutil.copy2(
        repo_root / "scripts" / "install-client.sh", alt_checkout / "scripts" / "install-client.sh"
    )
    shutil.copy2(repo_root / "docker-compose.yml", alt_checkout / "docker-compose.yml")
    shutil.copy2(
        repo_root / "client-runner" / "smartrss_client_runner.py",
        alt_checkout / "client-runner" / "smartrss_client_runner.py",
    )
    shutil.copy2(
        repo_root / "deploy" / "systemd" / "smartrss-client.service",
        alt_checkout / "deploy" / "systemd" / "smartrss-client.service",
    )
    shutil.copy2(
        repo_root / "deploy" / "systemd" / "smartrss-client.env.example",
        alt_checkout / "deploy" / "systemd" / "smartrss-client.env.example",
    )
    shutil.copy2(
        repo_root / "deploy" / "desktop" / "smartrss.desktop",
        alt_checkout / "deploy" / "desktop" / "smartrss.desktop",
    )

    # Stub check-prereqs.sh to succeed
    prereqs = alt_checkout / "scripts" / "check-prereqs.sh"
    prereqs.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    prereqs.chmod(prereqs.stat().st_mode | stat.S_IXUSR)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"

    # Stub docker recording all commands and simulating compose build/create/ps/cp/rm
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "docker $*" >> "{docker_log}"
if [[ "$1" == "compose" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == "-q" ]]; then
      echo "cid-alt-web-123"
      exit 0
    fi
  done
  exit 0
fi
if [[ "$1" == "cp" ]]; then
  dest="$3"
  mkdir -p "$dest"
  echo "<html>from-docker</html>" > "$dest/index.html"
  exit 0
fi
if [[ "$1" == "rm" ]]; then
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)

    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_systemctl.chmod(fake_systemctl.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")

    bash_bin = shutil.which("bash") or "/bin/bash"
    res = subprocess.run(  # noqa: S603
        [bash_bin, str(alt_checkout / "scripts" / "install-client.sh"), "--from-docker"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, (
        f"install-client.sh failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}"
    )

    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert any("compose" in c and "build web" in c for c in calls)
    assert any("compose" in c and "create --no-deps web" in c for c in calls)
    assert any("compose" in c and "ps -a -q web" in c for c in calls)
    assert any(c.startswith("docker cp cid-alt-web-123:/usr/share/nginx/html/.") for c in calls)
    assert any(c == "docker rm -f cid-alt-web-123" for c in calls)
    assert not any("smartrss-web:latest" in c for c in calls)
    installed_index = tmp_path / "xdg-data" / "smartrss" / "dist" / "index.html"
    assert installed_index.read_text(encoding="utf-8").strip() == "<html>from-docker</html>"


def test_v10_sw_origin_tag_sets_global_variable(live_server: runner.SmartRSSClientServer) -> None:
    """V-10: /sw.js prepends self.__SMARTRSS_ORIGIN_TAG__ matching the runner's origin_tag."""
    status, _, body = _request(live_server, "GET", "/sw.js")
    assert status == 200
    expected_prefix = f'self.__SMARTRSS_ORIGIN_TAG__="{live_server.origin_tag}";\n'.encode()
    assert body.startswith(expected_prefix)
