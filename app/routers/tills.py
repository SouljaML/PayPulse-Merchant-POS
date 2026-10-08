import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentUser, get_current_user, require_merchant_manager
from app.models import Device, DeviceStatus, Shop, Till, TillStatus
from app.schemas import TillCreate, TillDeviceSet, TillOut, TillStatusUpdate
from app.services import audit_service

router = APIRouter(prefix="/merchants/{merchant_id}/tills", tags=["tills"])


async def _till_out(db: AsyncSession, till: Till) -> TillOut:
    out = TillOut.model_validate(till)
    device = await db.scalar(select(Device).where(Device.till_id == till.id))
    if device is not None:
        out.device_id = device.id
        out.device_reference = device.reference
        out.device_label = device.label
        out.device_status = device.status.value
    return out


async def _claim_device(db: AsyncSession, merchant_id: uuid.UUID, device_id: uuid.UUID, shop_id: uuid.UUID) -> Device:
    device = await db.get(Device, device_id)
    if device is None or device.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.status == DeviceStatus.REVOKED:
        raise HTTPException(status_code=409, detail="This device has been revoked")
    if device.till_id is not None:
        raise HTTPException(status_code=409, detail="This device is already linked to a till")
    device.till_id = None  # set by caller once the till id is known
    device.shop_id = shop_id
    return device


@router.get("", response_model=list[TillOut])
async def list_tills(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Owners see every till; a teller sees only their own shop's tills."""
    if user.role != "platform_admin" and user.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    query = select(Till).where(Till.merchant_id == merchant_id)
    if user.role == "teller" and user.shop_id is not None:
        query = query.where(Till.shop_id == user.shop_id)

    tills = list(await db.scalars(query.order_by(Till.created_at.asc())))
    return [await _till_out(db, t) for t in tills]


@router.post("", response_model=TillOut, status_code=201)
async def create_till(
    merchant_id: uuid.UUID,
    body: TillCreate,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    shop = await db.get(Shop, body.shop_id)
    if shop is None or shop.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Shop not found")

    device = await _claim_device(db, merchant_id, body.device_id, body.shop_id)

    base = device.reference
    identifier, n = base, 1
    while await db.scalar(
        select(Till.id).where(Till.merchant_id == merchant_id, Till.till_identifier == identifier)
    ) is not None:
        n += 1
        identifier = f"{base}-{n}"

    till = Till(
        merchant_id=merchant_id,
        shop_id=body.shop_id,
        till_identifier=identifier,
        label=body.label,
        status=TillStatus.ACTIVE,
    )
    db.add(till)
    await db.flush()
    device.till_id = till.id

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="till.created",
        target_type="till",
        target_id=str(till.id),
        details={"shop_id": str(body.shop_id), "label": till.label, "device_id": str(device.id)},
    )

    await db.commit()
    return await _till_out(db, till)


@router.put("/{till_id}/device", response_model=TillOut)
async def set_till_device(
    merchant_id: uuid.UUID,
    till_id: uuid.UUID,
    body: TillDeviceSet,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    """Swap the device on a till. The old device becomes free again."""
    till = await db.get(Till, till_id)
    if till is None or till.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Till not found")

    old = await db.scalar(select(Device).where(Device.till_id == till.id))
    if old is not None and old.id == body.device_id:
        return await _till_out(db, till)

    new = await _claim_device(db, merchant_id, body.device_id, till.shop_id)
    if old is not None:
        old.till_id = None
        old.shop_id = None
        await db.flush()
    new.till_id = till.id

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="till.device_changed",
        target_type="till",
        target_id=str(till.id),
        details={"from": str(old.id) if old else None, "to": str(new.id)},
    )
    await db.commit()
    return await _till_out(db, till)


@router.patch("/{till_id}/status", response_model=TillOut)
async def set_till_status(
    merchant_id: uuid.UUID,
    till_id: uuid.UUID,
    body: TillStatusUpdate,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    """The actual fraud-response action: setting a till to blocked makes the
    next transaction attempt from it fail immediately (see the till check in
    transaction_service.initiate_transaction) — this is the real mechanism,
    not just a status label."""
    till = await db.get(Till, till_id)
    if till is None or till.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Till not found")

    try:
        new_status = TillStatus(body.status)
    except ValueError:
        raise HTTPException(status_code=400, detail="status must be 'active' or 'blocked'")

    if new_status == TillStatus.BLOCKED and not body.reason:
        raise HTTPException(status_code=400, detail="A reason is required when blocking a till")

    previous = till.status.value
    till.status = new_status
    till.blocked_reason = body.reason if new_status == TillStatus.BLOCKED else None

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="till.status_changed",
        target_type="till",
        target_id=str(till.id),
        details={"from": previous, "to": new_status.value, "reason": body.reason},
    )

    await db.commit()
    return await _till_out(db, till)
