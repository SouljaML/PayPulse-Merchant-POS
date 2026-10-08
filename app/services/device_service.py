import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceStatus

# No 0/O or 1/I — the code is read off a screen and typed on a phone.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
ENROLLMENT_TTL = timedelta(hours=24)
LAST_SEEN_REFRESH = timedelta(minutes=1)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def new_enrollment_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"


def new_device_token() -> str:
    return "ppd_" + secrets.token_urlsafe(32)


def issue_enrollment_code(device: Device) -> str:
    """Puts the device back to PENDING with a fresh code, and invalidates any
    credential it held before — so a lost or replaced phone stops working the
    moment the owner reissues, even if nobody remembers to revoke it."""
    code = new_enrollment_code()
    device.enrollment_code_hash = hash_secret(normalize_code(code))
    device.enrollment_expires_at = datetime.now(timezone.utc) + ENROLLMENT_TTL
    device.token_hash = None
    device.status = DeviceStatus.PENDING
    device.revoked_at = None
    device.revoked_reason = None
    return code


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_code_expired(device: Device, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return device.enrollment_expires_at is None or as_utc(device.enrollment_expires_at) < now


# ---- Brute-force guard for the unauthenticated enroll endpoint ----
# In-process and per-IP: good enough for one server. Move to Redis if this
# ever runs on several workers.
_MAX_FAILURES = 10
_WINDOW_SECONDS = 15 * 60
_failures: dict[str, list[float]] = {}


def enroll_blocked(client_key: str, now: float | None = None) -> bool:
    now = now or time.monotonic()
    recent = [t for t in _failures.get(client_key, []) if now - t < _WINDOW_SECONDS]
    _failures[client_key] = recent
    return len(recent) >= _MAX_FAILURES


def record_enroll_failure(client_key: str, now: float | None = None) -> None:
    _failures.setdefault(client_key, []).append(now or time.monotonic())


def clear_enroll_failures(client_key: str) -> None:
    _failures.pop(client_key, None)


async def find_active_device(db: AsyncSession, token: str | None) -> Device | None:
    if not token:
        return None
    device = await db.scalar(select(Device).where(Device.token_hash == hash_secret(token)))
    if device is None or device.status != DeviceStatus.ACTIVE:
        return None
    return device
