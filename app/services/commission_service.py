import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CommissionEntry,
    CommissionEntryType,
    CommissionType,
    ProviderCommissionRate,
    ProviderCommissionTier,
)

CENT = Decimal("0.01")


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


def _compute(commission_type: CommissionType, percentage: Decimal, flat_fee: Decimal, amount: Decimal) -> Decimal:
    if commission_type == CommissionType.PERCENTAGE:
        raw = amount * percentage
    elif commission_type == CommissionType.FLAT:
        raw = flat_fee
    else:  # PERCENTAGE_PLUS_FLAT
        raw = amount * percentage + flat_fee
    return raw.quantize(CENT, rounding=ROUND_HALF_UP)


def find_tier(tiers, amount: Decimal):
    """The band with min_amount <= amount <= max_amount (both inclusive; max
    NULL = unbounded), or None when the amount is in a gap between bands or
    above a capped top band."""
    for t in sorted(tiers, key=lambda t: t.min_amount):
        if amount >= t.min_amount and (t.max_amount is None or amount <= t.max_amount):
            return t
    return None


def tier_commission(tier) -> Decimal:
    """PayPulse's commission for a band: its share of the provider's fee plus
    any flat amount. Independent of the transaction amount itself — within a
    band the provider's fee is fixed, so the commission is too."""
    return (tier.provider_fee * tier.percentage + tier.flat_fee).quantize(CENT, rounding=ROUND_HALF_UP)


def commission_for(rate: ProviderCommissionRate, amount: Decimal) -> Decimal | None:
    """Commission PayPulse earns on `amount` under this rate version. A plain
    rate (no tiers) applies to every amount; a tiered one uses the matching
    band. Returns None when a tiered rate has no band for the amount — the
    caller records nothing, and the transaction shows up as 'uncommissioned'
    in reports rather than silently earning 0 or raising mid-confirmation."""
    if not rate.tiers:
        return _compute(rate.commission_type, rate.percentage, rate.flat_fee, amount)
    tier = find_tier(rate.tiers, amount)
    if tier is None:
        return None
    return tier_commission(tier)


def calculate_commission(rate: ProviderCommissionRate, transaction_amount: Decimal) -> Decimal:
    """Kept for existing callers; prefer commission_for(), which can say
    'no band for this amount'."""
    return commission_for(rate, transaction_amount) or Decimal("0.00")


async def set_commission_tiers(
    db: AsyncSession,
    *,
    provider_id: uuid.UUID,
    tiers: list,
    set_by: uuid.UUID,
) -> ProviderCommissionRate:
    """Same versioning rule as set_commission_rate: close the active version,
    insert a new one carrying the full new set of bands. `tiers` are already
    validated (CommissionTiersSet). The header's own type/percentage/flat
    columns are required by the table but ignored whenever tiers exist, so
    they're stored as zeros. Does NOT commit."""
    now = datetime.now(timezone.utc)

    current = await get_current_rate(db, provider_id)
    if current is not None:
        current.effective_to = now

    new_rate = ProviderCommissionRate(
        provider_id=provider_id,
        commission_type=CommissionType.PERCENTAGE,
        percentage=Decimal("0"),
        flat_fee=Decimal("0"),
        effective_from=now,
        set_by=set_by,
        tiers=[
            ProviderCommissionTier(
                min_amount=t.min_amount,
                max_amount=t.max_amount,
                provider_fee=t.provider_fee,
                commission_type=CommissionType.PERCENTAGE_PLUS_FLAT,  # legacy NOT NULL column; unused
                percentage=t.percentage,
                flat_fee=t.flat_fee,
            )
            for t in tiers
        ],
    )
    db.add(new_rate)
    await db.flush()
    return new_rate


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
