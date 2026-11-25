"""Integration tests for LAN security middleware, pairing auth, and device revocation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartrss.db.models import PairingCode
from smartrss.security.auth import create_pairing_code


async def test_host_allow_list_and_health_exemption(api_env: dict[str, Any]) -> None:
    transport = api_env["transport"]
    token = api_env["token"]

    async with httpx.AsyncClient(transport=transport, base_url="http://api:8000") as raw_client:
        # GET /api/v1/health is exempt from Host allow-list
        r_health = await raw_client.get("/api/v1/health", headers={"Host": "api:8000"})
        assert r_health.status_code == 200
        assert r_health.json()["db"] == "ok"

        # Disallowed Host rejected with 400 on protected AND public (/auth/pair) routes
        r_bad_host = await raw_client.get(
            "/api/v1/devices",
            headers={"Host": "evil.attacker.com", "Authorization": f"Bearer {token}"},
        )
        assert r_bad_host.status_code == 400

        r_bad_pair = await raw_client.post(
            "/api/v1/auth/pair",
            headers={"Host": "evil.attacker.com", "Content-Type": "application/json"},
            json={"code": "AAAA-BBBB-CCCC-DDDD"},
        )
        assert r_bad_pair.status_code == 400


async def test_cors_allow_list_and_json_mutation_guard(api_env: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api_env["client"]

    # Allowed origin gets Access-Control-Allow-Origin
    r_ok = await client.options(
        "/api/v1/categories",
        headers={
            "Origin": "http://localhost:8080",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert r_ok.status_code == 200
    assert r_ok.headers.get("access-control-allow-origin") == "http://localhost:8080"

    # Disallowed origin gets no Access-Control-Allow-Origin
    r_blocked = await client.options(
        "/api/v1/categories",
        headers={
            "Origin": "http://evil.example.net",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in r_blocked.headers

    # Non-JSON POSTs rejected with 415 Unsupported Media Type
    for ctype, body in (
        ("application/x-www-form-urlencoded", b"name=News"),
        ("text/plain", b"name=News"),
        ("multipart/form-data; boundary=xyz", b"--xyz--\r\n"),
    ):
        r_415 = await client.post(
            "/api/v1/categories",
            content=body,
            headers={"Content-Type": ctype},
        )
        assert r_415.status_code == 415

    # Body present without Content-Type -> 415
    r_missing_ct = await client.post("/api/v1/categories", content=b'{"name":"X"}')
    assert r_missing_ct.status_code == 415

    # Chunked transfer-encoding without Content-Type -> 415
    r_chunked = await client.post(
        "/api/v1/categories",
        headers={"Transfer-Encoding": "chunked"},
    )
    assert r_chunked.status_code == 415


async def test_pairing_bearer_auth_revocation_and_rate_limit(
    api_env: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client: httpx.AsyncClient = api_env["client"]
    transport = api_env["transport"]

    # Missing / invalid bearer -> 401
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost:8080",
        headers={"Host": "localhost:8080"},
    ) as anon:
        r_no_auth = await anon.get("/api/v1/devices")
        assert r_no_auth.status_code == 401
        assert r_no_auth.headers.get("www-authenticate") == "Bearer"

        r_bad_tok = await anon.get(
            "/api/v1/devices",
            headers={"Authorization": "Bearer totally-invalid-token"},
        )
        assert r_bad_tok.status_code == 401

        # Create a pairing code from authenticated device
        r_code = await client.post(
            "/api/v1/auth/pairing-codes",
            json={"device_name": "Pixel 9", "platform": "android"},
        )
        assert r_code.status_code == 201
        code = r_code.json()["code"]

        # Exchange code for bearer token
        r_pair = await anon.post(
            "/api/v1/auth/pair",
            json={"code": code, "device_name": "Pixel 9", "platform": "android"},
        )
        assert r_pair.status_code == 200
        paired = r_pair.json()
        new_token = paired["bearer_token"]
        new_device_id = paired["device_id"]

        # Reusing same code fails with 401
        r_reuse = await anon.post("/api/v1/auth/pair", json={"code": code})
        assert r_reuse.status_code == 401

        # Expired pairing code fails with 401
        async with session_factory() as s:
            exp = await create_pairing_code(s, device_name="ExpiredDev", platform="web")
            await s.execute(
                sa.update(PairingCode)
                .where(PairingCode.id == exp.record_id)
                .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            await s.commit()

        r_expired = await anon.post("/api/v1/auth/pair", json={"code": exp.code})
        assert r_expired.status_code == 401

        # Authenticated with new device token works; token_hash is never exposed
        r_devs = await anon.get(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert r_devs.status_code == 200
        assert all("token_hash" not in d for d in r_devs.json())

        # Revoke new device -> immediate 401
        r_revoke = await client.post(f"/api/v1/devices/{new_device_id}/revoke")
        assert r_revoke.status_code == 200
        assert r_revoke.json()["revoked_at"] is not None

        r_after_revoke = await anon.get(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert r_after_revoke.status_code == 401

        # Global sliding-window rate limit (5 / 5m): we already made 3 /auth/pair calls above
        for _ in range(2):
            r_more = await anon.post("/api/v1/auth/pair", json={"code": "0000-0000-0000-0000"})
            assert r_more.status_code == 401

        # 6th attempt -> 429 Too Many Requests with Retry-After
        r_429 = await anon.post("/api/v1/auth/pair", json={"code": "0000-0000-0000-0000"})
        assert r_429.status_code == 429
        assert int(r_429.headers["retry-after"]) >= 1
