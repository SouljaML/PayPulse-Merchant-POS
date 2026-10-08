import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentUser, get_current_user, require_merchant_manager, resolve_device
from app.models import Device, DeviceStatus, Shop, Till
from app.schemas import (
    DeviceCreate,
    DeviceEnrollOut,
    DeviceEnrollRequest,
    DeviceOut,
    DeviceRevoke,
    DeviceWithCodeOut,
)
from app.services import audit_service, device_service

# Owner / admin side: register, list, reissue, revoke.
router = APIRouter(prefix="/merchants/{merchant_id}/devices", tags=["devices"])
# Device side: enrol with a code, and ask "am I still allowed?".
device_router = APIRouter(prefix="/devices", tags=["devices"])


async def _names(db: AsyncSession, device: Device) -> tuple[str | None, str | None, str | None]:
    shop = await db.get(Shop, device.shop_id)
    till = await db.get(Till, device.till_id) if device.till_id else None
    return (shop.name if shop else None, till.label if till else None, till.till_identifier if till else None)


async def _out(db: AsyncSession, device: Device) -> DeviceOut:
    shop_name, till_label, _ = await _names(db, device)
    return DeviceOut.model_validate(device).model_copy(update={"shop_name": shop_name, "till_label": till_label})


@router.get("", response_model=list[DeviceOut])
async def list_devices(
    merchant_id: uuid.UUID,
    shop_id: uuid.UUID | None = None,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Device, Shop.name, Till.label)
        .join(Shop, Device.shop_id == Shop.id)
        .outerjoin(Till, Device.till_id == Till.id)
        .where(Device.merchant_id == merchant_id)
        .order_by(Device.created_at.desc())
    )
    if shop_id is not None:
        query = query.where(Device.shop_id == shop_id)
    rows = (await db.execute(query)).all()
    return [
        DeviceOut.model_validate(d).model_copy(update={"shop_name": shop_name, "till_label": till_label})
        for d, shop_name, till_label in rows
    ]


@router.post("", response_model=DeviceWithCodeOut, status_code=201)
async def register_device(
    merchant_id: uuid.UUID,
    body: DeviceCreate,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    shop = await db.get(Shop, body.shop_id)
    if shop is None or shop.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Shop not found")
    if body.till_id is not None:
        till = await db.get(Till, body.till_id)
        if till is None or till.merchant_id != merchant_id or till.shop_id != body.shop_id:
            raise HTTPException(status_code=404, detail="Till not found in this shop")

    device = Device(
        merchant_id=merchant_id,
        shop_id=body.shop_id,
        till_id=body.till_id,
        label=body.label.strip(),
        created_by=user.id,
    )
    code = device_service.issue_enrollment_code(device)
    db.add(device)
    await db.flush()

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="device.registered",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label, "shop_id": str(body.shop_id), "till_id": str(body.till_id) if body.till_id else None},
    )
    await db.commit()
    await db.refresh(device)
    return DeviceWithCodeOut(**(await _out(db, device)).model_dump(), enrollment_code=code)


async def _owned_device(db: AsyncSession, merchant_id: uuid.UUID, device_id: uuid.UUID) -> Device:
    device = await db.get(Device, device_id)
    if device is None or device.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.post("/{device_id}/reissue-code", response_model=DeviceWithCodeOut)
async def reissue_code(
    merchant_id: uuid.UUID,
    device_id: uuid.UUID,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    """For a replaced, reset or previously revoked phone. The old credential
    stops working immediately; the device is PENDING until it enrols again."""
    device = await _owned_device(db, merchant_id, device_id)
    code = device_service.issue_enrollment_code(device)
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="device.code_reissued",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label},
    )
    await db.commit()
    return DeviceWithCodeOut(**(await _out(db, device)).model_dump(), enrollment_code=code)


@router.post("/{device_id}/revoke", response_model=DeviceOut)
async def revoke_device(
    merchant_id: uuid.UUID,
    device_id: uuid.UUID,
    body: DeviceRevoke,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    device = await _owned_device(db, merchant_id, device_id)
    previous = device.status.value
    device.status = DeviceStatus.REVOKED
    device.revoked_at = datetime.now(timezone.utc)
    device.revoked_reason = body.reason
    device.token_hash = None
    device.enrollment_code_hash = None
    device.enrollment_expires_at = None
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="device.revoked",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label, "from": previous, "reason": body.reason},
    )
    await db.commit()
    return await _out(db, device)


# ---------------------------------------------------------------- device side


@device_router.post("/enroll", response_model=DeviceEnrollOut)
async def enroll_device(body: DeviceEnrollRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Unauthenticated by design: the one-time code IS the credential. Wrong
    codes are rate-limited per client address, and a code works once."""
    client_key = request.client.host if request.client else "unknown"
    if device_service.enroll_blocked(client_key):
        raise HTTPException(status_code=429, detail="Too many attempts. Wait a few minutes and try again.")

    invalid = HTTPException(status_code=400, detail="That code isn't valid or has expired. Ask your manager for a new one.")

    device = await db.scalar(
        select(Device).where(
            Device.enrollment_code_hash == device_service.hash_secret(device_service.normalize_code(body.code))
        )
    )
    if device is None or device.status != DeviceStatus.PENDING or device_service.is_code_expired(device):
        device_service.record_enroll_failure(client_key)
        raise invalid

    # The same physical phone can't be enrolled twice at once: that would be
    # one handset acting as two tills.
    if body.hardware_id:
        clash = await db.scalar(
            select(Device).where(
                Device.hardware_id == body.hardware_id,
                Device.status == DeviceStatus.ACTIVE,
                Device.id != device.id,
            )
        )
        if clash is not None:
            raise HTTPException(
                status_code=409, detail="This handset is already registered as another device. Revoke that one first."
            )

    token = device_service.new_device_token()
    now = datetime.now(timezone.utc)
    device.token_hash = device_service.hash_secret(token)
    device.status = DeviceStatus.ACTIVE
    device.enrollment_code_hash = None
    device.enrollment_expires_at = None
    device.enrolled_at = now
    device.last_seen_at = now
    device.hardware_id = body.hardware_id
    device.platform = body.platform
    device.model = body.model
    device.os_version = body.os_version
    device.app_version = body.app_version

    await audit_service.record(
        db,
        actor_user_id=device.created_by or uuid.UUID(int=0),
        action="device.enrolled",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label, "model": body.model, "platform": body.platform, "ip": client_key},
    )
    await db.commit()
    device_service.clear_enroll_failures(client_key)

    shop_name, till_label, till_identifier = await _names(db, device)
    return DeviceEnrollOut(
        device_id=device.id,
        device_token=token,
        label=device.label,
        merchant_id=device.merchant_id,
        shop_id=device.shop_id,
        shop_name=shop_name,
        till_id=device.till_id,
        till_identifier=till_identifier,
        till_label=till_label,
    )


@device_router.get("/me", response_model=DeviceEnrollOut)
async def device_me(request: Request, db: AsyncSession = Depends(get_db)):
    """Lets the app check on start-up whether it's still allowed. A revoked or
    unknown token gets 401 — the app then clears its credential and returns
    to the enrolment screen."""
    device = await resolve_device(request, db)
    if device is None:
        raise HTTPException(status_code=401, detail="This device is not registered or has been revoked")
    shop_name, till_label, till_identifier = await _names(db, device)
    return DeviceEnrollOut(
        device_id=device.id,
        device_token="",
        label=device.label,
        merchant_id=device.merchant_id,
        shop_id=device.shop_id,
        shop_name=shop_name,
        till_id=device.till_id,
        till_identifier=till_identifier,
        till_label=till_label,
    )
