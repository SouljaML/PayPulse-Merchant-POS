import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentUser, get_current_user, require_merchant_manager
from app.models import Shop, Till, TillStatus
from app.schemas import TillCreate, TillOut, TillStatusUpdate
from app.services import audit_service

router = APIRouter(prefix="/merchants/{merchant_id}/tills", tags=["tills"])


@router.get("", response_model=list[TillOut])
async def list_tills(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Merchant owners see every till across every shop; a teller sees only
    their own shop's tills — there's no reason a teller in Shop A needs
    visibility into Shop B's terminals."""
    if user.role != "platform_admin" and user.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    query = select(Till).where(Till.merchant_id == merchant_id)
    if user.role == "teller" and user.shop_id is not None:
        query = query.where(Till.shop_id == user.shop_id)

    tills = await db.scalars(query.order_by(Till.created_at.asc()))
    return list(tills)


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

    existing = await db.scalar(
        select(Till).where(Till.merchant_id == merchant_id, Till.till_identifier == body.till_identifier)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="A till with this identifier already exists for this merchant")

    till = Till(
        merchant_id=merchant_id,
        shop_id=body.shop_id,
        till_identifier=body.till_identifier,
        label=body.label,
        status=TillStatus.ACTIVE,
    )
    db.add(till)
    await db.flush()

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="till.created",
        target_type="till",
        target_id=str(till.id),
        details={"shop_id": str(body.shop_id), "label": till.label},
    )

    await db.commit()
    await db.refresh(till)
    return till


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
    return till
