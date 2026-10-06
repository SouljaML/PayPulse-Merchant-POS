import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CommissionEntry, CommissionEntryType, CommissionType, ProviderCommissionRate


async def get_current_rate(db: AsyncSession, provider_id: uuid.UUID) -> ProviderCommissionRate | None:
    """The active rate for a provider is the one row with effective_to IS NULL.
    Returns None if no rate has ever been set — callers should treat that as
    zero commission, not an error, since a brand-new provider legitimately
    starts with none configured."""
    return await db.scalar(
        select(ProviderCommissionRate).where(
            ProviderCommissionRate.provider_id == provider_id,
            ProviderCommissionRate.effective_to.is_(None),
        )
    )


async def set_commission_rate(
    db: AsyncSession,
    *,
    provider_id: uuid.UUID,
    commission_type: CommissionType,
    percentage: Decimal,
    flat_fee: Decimal,
    set_by: uuid.UUID,
) -> ProviderCommissionRate:
    """Closes out whatever rate is currently active (if any) and inserts the
    new one as of now. Never edits a rate row in place — that would silently
    rewrite history for every transaction that snapshotted it.

    Does NOT commit — the caller controls the transaction boundary, so it can
    add an audit log entry (or anything else) and commit both together
    atomically rather than as two separate, independently-failable writes."""
    now = datetime.now(timezone.utc)

    current = await get_current_rate(db, provider_id)
    if current is not None:
        current.effective_to = now

    new_rate = ProviderCommissionRate(
        provider_id=provider_id,
        commission_type=commission_type,
        percentage=percentage,
        flat_fee=flat_fee,
        effective_from=now,
        set_by=set_by,
    )
    db.add(new_rate)
    await db.flush()  # populates new_rate.id (a Python-side default, no commit needed for it)
    return new_rate


def calculate_commission(rate: ProviderCommissionRate, transaction_amount: Decimal) -> Decimal:
    if rate.commission_type == CommissionType.PERCENTAGE:
        return (transaction_amount * rate.percentage).quantize(Decimal("0.01"))
    if rate.commission_type == CommissionType.FLAT:
        return rate.flat_fee.quantize(Decimal("0.01"))
    # PERCENTAGE_PLUS_FLAT
    return (transaction_amount * rate.percentage + rate.flat_fee).quantize(Decimal("0.01"))


async def record_commission_entry(
    db: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    provider_id: uuid.UUID,
    commission_rate_id: uuid.UUID,
    amount: Decimal,
    entry_type: CommissionEntryType = CommissionEntryType.EARNED,
) -> CommissionEntry:
    """Writes one immutable ledger row. This is the audit/reconciliation
    record — always look here, not just at Transaction.commission_amount,
    when the question is "prove what we recorded for this transaction" rather
    than "quickly show the number in a list.\""""
    entry = CommissionEntry(
        transaction_id=transaction_id,
        provider_id=provider_id,
        commission_rate_id=commission_rate_id,
        entry_type=entry_type,
        amount=amount,
    )
    db.add(entry)
    await db.flush()
    return entry
