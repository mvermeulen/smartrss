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
