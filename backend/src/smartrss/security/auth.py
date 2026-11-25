"""Per-device pairing codes, bearer token hashing/verification, and rate limiting."""

from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smartrss.db.models import Device, PairingCode

DevicePlatform = Literal["ubuntu", "android", "web"]
VALID_PLATFORMS: frozenset[str] = frozenset({"ubuntu", "android", "web"})

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_SET = frozenset(CROCKFORD_ALPHABET)
_CROCKFORD_CONFUSABLES = str.maketrans({"O": "0", "I": "1", "L": "1"})

PAIRING_CODE_LENGTH = 16
PAIRING_CODE_TTL_MINUTES = 5


class InvalidPairingCodeError(ValueError):
    """Raised when a pairing code is malformed, expired, unknown, or already consumed."""


def hash_secret(raw: str) -> str:
    """Return hex SHA-256 digest of a bearer token or normalized pairing code."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_bearer_token() -> str:
    """Generate a 256-bit URL-safe bearer token."""
    return secrets.token_urlsafe(32)


def generate_pairing_code() -> str:
    """Generate a 16-char Crockford Base32 pairing code formatted XXXX-XXXX-XXXX-XXXX."""
    chars = "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
    return f"{chars[0:4]}-{chars[4:8]}-{chars[8:12]}-{chars[12:16]}"


def normalize_pairing_code(raw: str) -> str:
    """Strip hyphens/spaces, uppercase, map Crockford confusables, and validate."""
    cleaned = "".join(ch for ch in raw.strip().upper() if ch not in ("-", " ", "\t", "\n"))
    cleaned = cleaned.translate(_CROCKFORD_CONFUSABLES)
    if len(cleaned) != PAIRING_CODE_LENGTH or any(ch not in _CROCKFORD_SET for ch in cleaned):
        raise InvalidPairingCodeError("Malformed pairing code")
    return cleaned


class GlobalSlidingWindowRateLimiter:
    """Single global sliding-window rate limiter for /api/v1/auth/pair (5 attempts / 5m)."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._clock = clock or time.monotonic
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def check_and_record(self) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        async with self._lock:
            now = self._clock()
            cutoff = now - self._window_seconds
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._max_attempts:
                retry_after = max(1, math.ceil(self._timestamps[0] + self._window_seconds - now))
                return False, retry_after

            self._timestamps.append(now)
            return True, 0

    def reset(self) -> None:
        """Clear recorded attempts (useful in tests)."""
        self._timestamps.clear()


@dataclass(frozen=True, slots=True)
class CreatedPairingCode:
    code: str
    expires_at: datetime
    record_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PairedDeviceResult:
    device: Device
    bearer_token: str


async def create_pairing_code(
    session: AsyncSession,
    *,
    device_name: str,
    platform: str = "ubuntu",
    created_by_device_id: uuid.UUID | None = None,
    ttl_minutes: int = PAIRING_CODE_TTL_MINUTES,
) -> CreatedPairingCode:
    """Create and persist a one-time pairing code (stored hashed)."""
    if platform not in VALID_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform!r}")
    code = generate_pairing_code()
    normalized = normalize_pairing_code(code)
    expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    row = PairingCode(
        id=uuid.uuid4(),
        code_hash=hash_secret(normalized),
        device_name=device_name.strip(),
        platform=platform,
        expires_at=expires_at,
        created_by_device_id=created_by_device_id,
    )
    session.add(row)
    await session.flush()
    return CreatedPairingCode(code=code, expires_at=expires_at, record_id=row.id)


async def redeem_pairing_code(
    session: AsyncSession,
    *,
    raw_code: str,
    device_name: str | None = None,
    platform: str | None = None,
) -> PairedDeviceResult:
    """Atomically consume a valid pairing code and issue a new per-device bearer token."""
    normalized = normalize_pairing_code(raw_code)
    code_hash = hash_secret(normalized)

    result = await session.execute(
        sa.update(PairingCode)
        .where(
            PairingCode.code_hash == code_hash,
            PairingCode.consumed_at.is_(None),
            PairingCode.expires_at > sa.func.now(),
        )
        .values(consumed_at=sa.func.now())
        .returning(PairingCode.id, PairingCode.device_name, PairingCode.platform)
    )
    consumed = result.one_or_none()
    if consumed is None:
        raise InvalidPairingCodeError("Invalid, expired, or already used pairing code")

    effective_name = (device_name or consumed.device_name).strip()
    effective_platform = platform or consumed.platform
    if effective_platform not in VALID_PLATFORMS:
        raise ValueError(f"Unsupported platform: {effective_platform!r}")

    bearer_token = generate_bearer_token()
    device = Device(
        id=uuid.uuid4(),
        name=effective_name,
        platform=effective_platform,
        token_hash=hash_secret(bearer_token),
    )
    session.add(device)
    await session.flush()
    return PairedDeviceResult(device=device, bearer_token=bearer_token)


async def authenticate_bearer_token(
    session: AsyncSession,
    raw_token: str,
    *,
    touch_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Device | None:
    """Verify bearer token hash, reject revoked devices, and lazily bump last_seen_at.

    When touch_session_factory is provided (I-24), the last_seen_at UPDATE commits on a
    separate short session so the request's shared DB session is never implicitly committed.
    """
    if not raw_token:
        return None
    token_hash = hash_secret(raw_token)
    result = await session.execute(
        sa.select(Device).where(Device.token_hash == token_hash, Device.revoked_at.is_(None))
    )
    device = result.scalar_one_or_none()
    if device is None:
        return None

    now = datetime.now(UTC)
    if device.last_seen_at is None or (now - device.last_seen_at) >= timedelta(seconds=60):
        stmt = (
            sa.update(Device)
            .where(
                Device.id == device.id,
                sa.or_(
                    Device.last_seen_at.is_(None),
                    Device.last_seen_at < now - timedelta(seconds=60),
                ),
            )
            .values(last_seen_at=now)
        )
        if touch_session_factory is not None:
            async with touch_session_factory() as touch_session:
                await touch_session.execute(stmt)
                await touch_session.commit()
        else:
            await session.execute(stmt)
            await session.commit()
        device.last_seen_at = now
    return device


async def revoke_device(session: AsyncSession, device_id: uuid.UUID) -> Device | None:
    """Mark a device revoked immediately."""
    result = await session.execute(
        sa.update(Device)
        .where(Device.id == device_id)
        .values(revoked_at=sa.func.coalesce(Device.revoked_at, sa.func.now()))
        .returning(Device)
    )
    return result.scalar_one_or_none()
