import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.database import get_db
from app.dependencies import CurrentUser, require_roles
from app.models import Role, User
from app.schemas import StaffUserCreate, StaffUserOut, StaffUserStatusUpdate
from app.services import audit_service

router = APIRouter(prefix="/users", tags=["users"])

# Only platform staff roles are manageable here — merchant_owner/teller
# belong to a merchant and are created as part of merchant onboarding, not
# through team management. Restricting the role field to this set stops
# someone from being created as a "merchant_owner" with no merchant_id
# attached, which would be a user nothing can actually scope correctly.
STAFF_ROLES = {"platform_admin", "compliance_officer"}


@router.get("", response_model=list[StaffUserOut])
async def list_staff_users(
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(User, Role.name)
        .join(Role, User.role_id == Role.id)
        .where(Role.name.in_(STAFF_ROLES))
        .order_by(User.created_at.asc())
    )
    return [
        StaffUserOut(
            id=u.id,
            email=u.email,
            full_name=u.full_name,
            role=role_name,
            is_active=u.is_active,
            created_at=u.created_at,
        )
        for u, role_name in rows.all()
    ]


@router.post("", response_model=StaffUserOut, status_code=201)
async def create_staff_user(
    body: StaffUserCreate,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    if body.role not in STAFF_ROLES:
        raise HTTPException(
            status_code=400, detail=f"role must be one of: {', '.join(sorted(STAFF_ROLES))}"
        )

    existing = await db.scalar(select(User).where(User.email == body.email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    role = await db.scalar(select(Role).where(Role.name == body.role))
    if role is None:
        raise HTTPException(status_code=500, detail=f"Role '{body.role}' is not seeded — run seed_dev_data.py")

    new_user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        role_id=role.id,
        merchant_id=None,
    )
    db.add(new_user)
    await db.flush()

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="staff_user.created",
        target_type="user",
        target_id=str(new_user.id),
        details={"email": new_user.email, "role": role.name},
    )

    await db.commit()
    await db.refresh(new_user)

    return StaffUserOut(
        id=new_user.id,
        email=new_user.email,
        full_name=new_user.full_name,
        role=role.name,
        is_active=new_user.is_active,
        created_at=new_user.created_at,
    )


@router.patch("/{user_id}/status", response_model=StaffUserOut)
async def set_staff_user_status(
    user_id: uuid.UUID,
    body: StaffUserStatusUpdate,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Deactivating a login is preferred over deleting the user row for the
    same reason merchants are suspended rather than deleted — every action
    that user has ever taken (KYC approvals, commission rate changes) stays
    attributable to a real row rather than a dangling foreign key."""
    if user_id == user.id and not body.is_active:
        raise HTTPException(status_code=400, detail="You can't deactivate your own account")

    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    role = await db.get(Role, target.role_id)
    if role is None or role.name not in STAFF_ROLES:
        raise HTTPException(status_code=404, detail="User not found")

    target.is_active = body.is_active

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="staff_user.status_changed",
        target_type="user",
        target_id=str(target.id),
        details={"is_active": body.is_active},
    )

    await db.commit()

    return StaffUserOut(
        id=target.id,
        email=target.email,
        full_name=target.full_name,
        role=role.name,
        is_active=target.is_active,
        created_at=target.created_at,
    )
