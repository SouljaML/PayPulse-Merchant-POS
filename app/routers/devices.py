import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentUser, require_merchant_manager, require_roles, resolve_device
from app.models import Device, DeviceStatus, Merchant, Shop, Till
from app.schemas import (
    DeviceAssign,
    DeviceCreate,
    DeviceEnrollOut,
    DeviceEnrollRequest,
    DeviceOut,
    DeviceReason,
    DeviceWithCodeOut,
)
from app.services import audit_service, device_service

# PayPulse side: the whole fleet — add, assign to merchants, suspend, revoke.
admin_router = APIRouter(prefix="/admin/devices", tags=["devices"])
# Merchant side: read-only view of the devices PayPulse has assigned to them.
router = APIRouter(prefix="/merchants/{merchant_id}/devices", tags=["devices"])
# Device side: enrol with a code, and ask "am I still allowed?".
device_router = APIRouter(prefix="/devices", tags=["devices"])

admin_only = require_roles("platform_admin")


async def _out(db: AsyncSession, device: Device) -> DeviceOut:
    merchant = await db.get(Merchant, device.merchant_id) if device.merchant_id else None
    shop = await db.get(Shop, device.shop_id) if device.shop_id else None
    till = await db.get(Till, device.till_id) if device.till_id else None
    return DeviceOut.model_validate(device).model_copy(
        update={
            "merchant_name": merchant.trading_name if merchant else None,
            "shop_name": shop.name if shop else None,
            "till_label": till.label if till else None,
        }
    )


def _rows_to_out(rows) -> list[DeviceOut]:
    return [
        DeviceOut.model_validate(d).model_copy(
            update={"merchant_name": merchant_name, "shop_name": shop_name, "till_label": till_label}
        )
        for d, merchant_name, shop_name, till_label in rows
    ]


def _joined_query():
    return (
        select(Device, Merchant.trading_name, Shop.name, Till.label)
        .outerjoin(Merchant, Device.merchant_id == Merchant.id)
        .outerjoin(Shop, Device.shop_id == Shop.id)
        .outerjoin(Till, Device.till_id == Till.id)
        .order_by(Device.created_at.desc())
    )


async def _get_device(db: AsyncSession, device_id: uuid.UUID) -> Device:
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


# ------------------------------------------------------------------ PayPulse


@admin_router.get("", response_model=list[DeviceOut])
async def admin_list_devices(
    status: DeviceStatus | None = Query(None),
    merchant_id: uuid.UUID | None = Query(None),
    unassigned: bool = Query(False, description="Only devices still in PayPulse's stock"),
    user: CurrentUser = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    query = _joined_query()
    if status is not None:
        query = query.where(Device.status == status)
    if merchant_id is not None:
        query = query.where(Device.merchant_id == merchant_id)
    if unassigned:
        query = query.where(Device.merchant_id.is_(None))
    return _rows_to_out((await db.execute(query)).all())


@admin_router.post("", response_model=DeviceWithCodeOut, status_code=201)
async def add_device(
    body: DeviceCreate,
    user: CurrentUser = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    """Adds a device to PayPulse's inventory and returns the one-time
    enrolment code used to set it up (normally at the office)."""
    serial = (body.serial_number or "").strip() or None
    if serial is not None:
        taken = await db.scalar(select(Device).where(Device.serial_number == serial))
        if taken is not None:
            raise HTTPException(status_code=409, detail="A device with this serial number already exists")

    device = Device(label=body.label.strip(), serial_number=serial, created_by=user.id)
    code = device_service.issue_enrollment_code(device)
    db.add(device)
    await db.flush()
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="device.added",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label, "serial_number": serial},
    )
    await db.commit()
    await db.refresh(device)
    return DeviceWithCodeOut(**(await _out(db, device)).model_dump(), enrollment_code=code)


@admin_router.post("/{device_id}/assign", response_model=DeviceOut)
async def assign_device(
    device_id: uuid.UUID,
    body: DeviceAssign,
    user: CurrentUser = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    """Hands the device to a merchant. The merchant's owner then links it to
    one of their tills; until they do, it stays inert."""
    device = await _get_device(db, device_id)
    if device.status == DeviceStatus.REVOKED:
        raise HTTPException(status_code=409, detail="This device is revoked. Issue a new code first")
    if device.merchant_id is not None and device.merchant_id != body.merchant_id:
        raise HTTPException(status_code=409, detail="This device is assigned to another merchant. Unassign it first")
    merchant = await db.get(Merchant, body.merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    if device.merchant_id is None:
        device.merchant_id = merchant.id
        device.assigned_at = datetime.now(timezone.utc)
        await audit_service.record(
            db,
            actor_user_id=user.id,
            action="device.assigned",
            target_type="device",
            target_id=str(device.id),
            details={"label": device.label, "merchant_id": str(merchant.id), "merchant": merchant.trading_name},
        )
        await db.commit()
    return await _out(db, device)


@admin_router.post("/{device_id}/unassign", response_model=DeviceOut)
async def unassign_device(
    device_id: uuid.UUID,
    user: CurrentUser = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    """Takes the device back into PayPulse's stock (and off its till)."""
    device = await _get_device(db, device_id)
    previous = device.merchant_id
    device.merchant_id = None
    device.shop_id = None
    device.till_id = None
    device.assigned_at = None
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="device.unassigned",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label, "merchant_id": str(previous) if previous else None},
    )
    await db.commit()
    return await _out(db, device)


@admin_router.post("/{device_id}/suspend", response_model=DeviceOut)
async def suspend_device(
    device_id: uuid.UUID,
    body: DeviceReason,
    user: CurrentUser = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    """For an unpaid lease or similar: stops the device on its next request but
    keeps its credential, so reinstating it is instant."""
    device = await _get_device(db, device_id)
    if device.status != DeviceStatus.ACTIVE:
        raise HTTPException(status_code=409, detail=f"Only an active device can be suspended (this one is {device.status.value})")
    device.status = DeviceStatus.SUSPENDED
    device.suspended_at = datetime.now(timezone.utc)
    device.suspended_reason = body.reason
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="device.suspended",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label, "reason": body.reason},
    )
    await db.commit()
    return await _out(db, device)


@admin_router.post("/{device_id}/reinstate", response_model=DeviceOut)
async def reinstate_device(
    device_id: uuid.UUID,
    user: CurrentUser = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device(db, device_id)
    if device.status != DeviceStatus.SUSPENDED:
        raise HTTPException(status_code=409, detail="This device isn't suspended")
    device.status = DeviceStatus.ACTIVE
    device.suspended_at = None
    device.suspended_reason = None
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="device.reinstated",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label},
    )
    await db.commit()
    return await _out(db, device)


@admin_router.post("/{device_id}/revoke", response_model=DeviceOut)
async def revoke_device(
    device_id: uuid.UUID,
    body: DeviceReason,
    user: CurrentUser = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    """For a lost, stolen or returned device: destroys its credential and frees
    its till. Bringing it back needs a new enrolment code."""
    device = await _get_device(db, device_id)
    previous = device.status.value
    device.status = DeviceStatus.REVOKED
    device.revoked_at = datetime.now(timezone.utc)
    device.revoked_reason = body.reason
    device.token_hash = None
    device.enrollment_code_hash = None
    device.enrollment_expires_at = None
    device.suspended_at = None
    device.suspended_reason = None
    device.till_id = None
    device.shop_id = None
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


@admin_router.post("/{device_id}/reissue-code", response_model=DeviceWithCodeOut)
async def reissue_code(
    device_id: uuid.UUID,
    user: CurrentUser = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    """For a replaced, reset or previously revoked handset. The old credential
    stops working immediately; the device stays assigned but inert until it
    enrols again."""
    device = await _get_device(db, device_id)
    was_revoked = device.status == DeviceStatus.REVOKED
    if was_revoked:
        # A revoked device comes back as fresh inventory: no merchant, no till,
        # so it can be assigned to anyone once it has enrolled again.
        device.merchant_id = None
        device.shop_id = None
        device.till_id = None
        device.assigned_at = None
    code = device_service.issue_enrollment_code(device)
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="device.returned_to_inventory" if was_revoked else "device.code_reissued",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label},
    )
    await db.commit()
    return DeviceWithCodeOut(**(await _out(db, device)).model_dump(), enrollment_code=code)


# ------------------------------------------------------------------ merchant


@router.get("", response_model=list[DeviceOut])
async def list_merchant_devices(
    merchant_id: uuid.UUID,
    available: bool = Query(False, description="Only devices not yet linked to a till"),
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    """The devices PayPulse has assigned to this merchant. Read-only: owners
    link them to tills (Tills page) but can't add, move or remove them."""
    query = _joined_query().where(Device.merchant_id == merchant_id)
    if available:
        query = query.where(Device.till_id.is_(None), Device.status != DeviceStatus.REVOKED)
    return _rows_to_out((await db.execute(query)).all())


# ---------------------------------------------------------------- device side


async def _enroll_out(db: AsyncSession, device: Device, token: str) -> DeviceEnrollOut:
    shop = await db.get(Shop, device.shop_id) if device.shop_id else None
    till = await db.get(Till, device.till_id) if device.till_id else None
    return DeviceEnrollOut(
        device_id=device.id,
        device_token=token,
        label=device.label,
        status=device.status.value,
        assigned=device.merchant_id is not None and device.till_id is not None,
        merchant_id=device.merchant_id,
        shop_id=device.shop_id,
        shop_name=shop.name if shop else None,
        till_id=device.till_id,
        till_identifier=till.till_identifier if till else None,
        till_label=till.label if till else None,
    )


@device_router.post("/enroll", response_model=DeviceEnrollOut)
async def enroll_device(body: DeviceEnrollRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Unauthenticated by design: the one-time code IS the credential. Wrong
    codes are rate-limited per client address, and a code works once."""
    client_key = request.client.host if request.client else "unknown"
    if device_service.enroll_blocked(client_key):
        raise HTTPException(status_code=429, detail="Too many attempts. Wait a few minutes and try again.")

    invalid = HTTPException(status_code=400, detail="That code isn't valid or has expired. Ask PayPulse for a new one.")

    device = await db.scalar(
        select(Device).where(
            Device.enrollment_code_hash == device_service.hash_secret(device_service.normalize_code(body.code))
        )
    )
    if device is None or device.status != DeviceStatus.PENDING or device_service.is_code_expired(device):
        device_service.record_enroll_failure(client_key)
        raise invalid

    # The same physical handset can't be enrolled twice at once: that would be
    # one handset acting as two devices.
    if body.hardware_id:
        clash = await db.scalar(
            select(Device).where(
                Device.hardware_id == body.hardware_id,
                Device.status.in_((DeviceStatus.ACTIVE, DeviceStatus.SUSPENDED)),
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
        actor_user_id=device.created_by,
        action="device.enrolled",
        target_type="device",
        target_id=str(device.id),
        details={"label": device.label, "model": body.model, "platform": body.platform, "ip": client_key},
    )
    await db.commit()
    device_service.clear_enroll_failures(client_key)
    return await _enroll_out(db, device, token)


@device_router.get("/me", response_model=DeviceEnrollOut)
async def device_me(request: Request, db: AsyncSession = Depends(get_db)):
    """Lets the app check on start-up where it stands: active and assigned,
    suspended, or waiting to be assigned. A revoked or unknown token gets 401
    and the app returns to the enrolment screen."""
    device = await resolve_device(request, db)
    if device is None:
        raise HTTPException(status_code=401, detail="This device is not registered or has been revoked")
    return await _enroll_out(db, device, "")
