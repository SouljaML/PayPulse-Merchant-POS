import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def record(
    db: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: str,
    details: dict | None = None,
) -> None:
    """Adds one audit log row. Deliberately does NOT call db.commit() itself —
    the caller is already mid-transaction doing the real action (approving a
    document, changing a rate), and the audit entry should commit atomically
    with that action, not as a separate round trip that could succeed or fail
    independently of it."""
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
        )
    )
