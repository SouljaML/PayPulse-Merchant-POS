from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, Query

from app.database import get_db
from app.dependencies import CurrentUser, require_roles
from app.models import AuditLog, User
from app.schemas import AuditLogOut

router = APIRouter(prefix="/audit-log", tags=["audit"])


@router.get("", response_model=list[AuditLogOut])
async def list_audit_log(
    target_type: str | None = Query(None),
    target_id: str | None = Query(None),
    limit: int = Query(100, le=500),
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    """Every admin action recorded here — merchant/provider/commission/staff
    changes, KYC decisions. Nothing about who did what to whom should ever be
    only reconstructable from memory; this is the actual record."""
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if target_type:
        query = query.where(AuditLog.target_type == target_type)
    if target_id:
        query = query.where(AuditLog.target_id == target_id)

    rows = await db.scalars(query)
    results = []
    for row in rows:
        actor_email = None
        if row.actor_user_id:
            actor = await db.get(User, row.actor_user_id)
            actor_email = actor.email if actor else None
        results.append(
            AuditLogOut(
                id=row.id,
                actor_user_id=row.actor_user_id,
                actor_email=actor_email,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                details=row.details,
                created_at=row.created_at,
            )
        )
    return results
