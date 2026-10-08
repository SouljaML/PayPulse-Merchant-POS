import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import Device, DeviceStatus, User
from app.services import device_service

settings = get_settings()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


DEVICE_HEADER = "x-device-token"


def _device_error(code: str, message: str) -> HTTPException:
    # `code` lets the POS app tell "this phone isn't registered" apart from
    # any other 403 and send the person to the enrolment screen.
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": code, "message": message})


async def resolve_device(request: Request, db: AsyncSession) -> Device | None:
    """The device named by the request's X-Device-Token header (active or
    suspended), or None. Whether it may actually do anything is decided by
    check_device_scope."""
    device = await device_service.find_device_by_token(db, request.headers.get(DEVICE_HEADER))
    if device is None:
        return None
    now = datetime.now(timezone.utc)
    if device.last_seen_at is None or now - device_service.as_utc(device.last_seen_at) > device_service.LAST_SEEN_REFRESH:
        await db.execute(update(Device).where(Device.id == device.id).values(last_seen_at=now))
        await db.commit()
    return device


def check_device_scope(device: Device | None, *, merchant_id: uuid.UUID | None, shop_id: uuid.UUID | None) -> Device:
    """A device must exist, be active, and belong to the same merchant (and,
    for a teller pinned to one shop, the same shop) as the person using it."""
    if device is None:
        raise _device_error(
            "device_not_registered",
            "This device isn't registered with PayPulse. Contact PayPulse to have it set up.",
        )
    if device.status == DeviceStatus.SUSPENDED:
        raise _device_error("device_suspended", "This device has been suspended. Contact PayPulse.")
    if device.status != DeviceStatus.ACTIVE:
        raise _device_error("device_not_registered", "This device isn't registered with PayPulse.")
    if device.merchant_id is None or device.till_id is None:
        raise _device_error(
            "device_unassigned",
            "This device hasn't been assigned to a till yet. Ask your manager, or contact PayPulse.",
        )
    if merchant_id is None or device.merchant_id != merchant_id:
        raise _device_error("device_wrong_merchant", "This device belongs to a different merchant.")
    if shop_id is not None and device.shop_id != shop_id:
        raise _device_error("device_wrong_shop", "This device is registered to a different shop.")
    return device


@dataclass
class CurrentUser:
    id: uuid.UUID
    merchant_id: uuid.UUID | None
    shop_id: uuid.UUID | None
    role: str


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_error
    except JWTError:
        raise credentials_error

    # The token proves who logged in, not that the account is still allowed
    # to act. Check the row itself, so deactivating someone takes effect on
    # their very next request instead of whenever their token expires.
    db_user = await db.get(User, uuid.UUID(user_id))
    if db_user is None or not db_user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="This account has been deactivated")
    if db_user.must_change_password and request.url.path != "/auth/change-password":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You need to change your temporary password before you can continue",
        )

    merchant_id = payload.get("merchant_id")
    shop_id = payload.get("shop_id")

    # A teller only ever works from the POS, so every teller request must come
    # from a registered, active device. Owners and admins use the web portal
    # from any browser and aren't held to this (they are held to it when they
    # transact — see require_device).
    if settings.require_registered_devices and payload.get("role") == "teller":
        device = await resolve_device(request, db)
        check_device_scope(
            device,
            merchant_id=uuid.UUID(merchant_id) if merchant_id else None,
            shop_id=uuid.UUID(shop_id) if shop_id else None,
        )

    return CurrentUser(
        id=uuid.UUID(user_id),
        merchant_id=uuid.UUID(merchant_id) if merchant_id else None,
        shop_id=uuid.UUID(shop_id) if shop_id else None,
        role=payload.get("role", ""),
    )


def require_roles(*allowed_roles: str):
    """Usage: Depends(require_roles('platform_admin', 'compliance_officer'))"""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return _check


async def require_merchant_scope(
    merchant_id: uuid.UUID, user: CurrentUser = Depends(get_current_user)
) -> CurrentUser:
    """Ensures a merchant-portal user can only ever act on their own merchant_id,
    even if platform_admin routes are structurally similar."""
    if user.role == "platform_admin":
        return user
    if user.merchant_id != merchant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this merchant")
    return user


async def require_merchant_manager(
    merchant_id: uuid.UUID, user: CurrentUser = Depends(get_current_user)
) -> CurrentUser:
    """Stricter than require_merchant_scope: shop/till/teller management is
    an owner-level action, not something any logged-in teller of the same
    merchant should be able to do just because they belong to it."""
    if user.role == "platform_admin":
        return user
    if user.role == "merchant_owner" and user.merchant_id == merchant_id:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Only the merchant owner or a platform admin can do this"
    )


async def require_device(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Device | None:
    """For anything that moves money: the caller's device must be registered
    and active, whatever their role. (Returns None only when enforcement is
    switched off for local development.)"""
    if not settings.require_registered_devices:
        return await resolve_device(request, db)
    device = await resolve_device(request, db)
    return check_device_scope(device, merchant_id=user.merchant_id, shop_id=user.shop_id)
