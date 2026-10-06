import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.database import get_db
from app.dependencies import CurrentUser, get_current_user, require_merchant_manager
from app.models import Role, Shop, User
from app.schemas import TellerCreate, TellerOut, TellerShopAssign, TellerStatusUpdate
from app.services import audit_service

router = APIRouter(prefix="/merchants/{merchant_id}/tellers", tags=["tellers"])

# A merchant owner can add another owner (a co-owner) or a teller — never a
# platform staff role. That boundary is enforced here, separately from
# users.py's STAFF_ROLES check, because these are two genuinely different
# kinds of account: platform staff work for PayPulse, these work for the
# merchant.
MERCHANT_ROLES = {"merchant_owner", "teller"}


def _to_teller_out(user: User, role_name: str) -> TellerOut:
    return TellerOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=role_name,
        shop_id=user.shop_id,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.get("", response_model=list[TellerOut])
async def list_tellers(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role != "platform_admin" and user.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    rows = await db.execute(
        select(User, Role.name)
        .join(Role, User.role_id == Role.id)
        .where(User.merchant_id == merchant_id, Role.name.in_(MERCHANT_ROLES))
        .order_by(User.created_at.asc())
    )
    return [_to_teller_out(u, role_name) for u, role_name in rows.all()]


@router.post("", response_model=TellerOut, status_code=201)
async def create_teller(
    merchant_id: uuid.UUID,
    body: TellerCreate,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    if body.role not in MERCHANT_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of: {', '.join(sorted(MERCHANT_ROLES))}")

    if body.shop_id is not None:
        shop = await db.get(Shop, body.shop_id)
        if shop is None or shop.merchant_id != merchant_id:
            raise HTTPException(status_code=404, detail="Shop not found")

    existing = await db.scalar(select(User).where(User.email == body.email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    role = await db.scalar(select(Role).where(Role.name == body.role))
    if role is None:
        raise HTTPException(status_code=500, detail=f"Role '{body.role}' is not seeded")

    new_user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        role_id=role.id,
        merchant_id=merchant_id,
        shop_id=body.shop_id,
    )
    db.add(new_user)
    await db.flush()

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="teller.created",
        target_type="user",
        target_id=str(new_user.id),
        details={"email": new_user.email, "role": role.name, "shop_id": str(body.shop_id) if body.shop_id else None},
    )

    await db.commit()
    await db.refresh(new_user)
    return _to_teller_out(new_user, role.name)


@router.patch("/{user_id}/status", response_model=TellerOut)
async def set_teller_status(
    merchant_id: uuid.UUID,
    user_id: uuid.UUID,
    body: TellerStatusUpdate,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    if user_id == user.id and not body.is_active:
        raise HTTPException(status_code=400, detail="You can't deactivate your own account")

    target = await db.get(User, user_id)
    if target is None or target.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="User not found")

    role = await db.get(Role, target.role_id)
    if role is None or role.name not in MERCHANT_ROLES:
        raise HTTPException(status_code=404, detail="User not found")

    target.is_active = body.is_active

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="teller.status_changed",
        target_type="user",
        target_id=str(target.id),
        details={"is_active": body.is_active},
    )

    await db.commit()
    return _to_teller_out(target, role.name)


@router.patch("/{user_id}/shop", response_model=TellerOut)
async def assign_teller_shop(
    merchant_id: uuid.UUID,
    user_id: uuid.UUID,
    body: TellerShopAssign,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, user_id)
    if target is None or target.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="User not found")

    role = await db.get(Role, target.role_id)
    if role is None or role.name not in MERCHANT_ROLES:
        raise HTTPException(status_code=404, detail="User not found")

    if body.shop_id is not None:
        shop = await db.get(Shop, body.shop_id)
        if shop is None or shop.merchant_id != merchant_id:
            raise HTTPException(status_code=404, detail="Shop not found")

    previous_shop = str(target.shop_id) if target.shop_id else None
    target.shop_id = body.shop_id

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="teller.shop_reassigned",
        target_type="user",
        target_id=str(target.id),
        details={"from_shop": previous_shop, "to_shop": str(body.shop_id) if body.shop_id else None},
    )

    await db.commit()
    return _to_teller_out(target, role.name)
