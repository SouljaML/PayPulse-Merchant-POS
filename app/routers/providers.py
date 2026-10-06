import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import get_adapter_registry
from app.database import get_db
from app.dependencies import CurrentUser, require_roles
from app.models import CommissionEntry, Provider, ProviderCommissionRate, ProviderStatus
from app.schemas import (
    CommissionEntryOut,
    CommissionRateCreate,
    CommissionRateOut,
    CommissionSummaryOut,
    ProviderCreate,
    ProviderOut,
)
from app.services import audit_service, commission_service

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=list[ProviderOut])
async def list_providers(db: AsyncSession = Depends(get_db)):
    """requires_otp isn't a database column — it's a property of whichever
    adapter class is registered for this provider's adapter_key (see
    BaseProviderAdapter.requires_otp). Looked up from the live adapter
    registry at request time rather than duplicated into the Provider table,
    so it can never drift out of sync with the adapter actually in use. A
    provider whose adapter_key has no registered adapter (shouldn't happen —
    create_provider() rejects that — but belt-and-suspenders) defaults to
    False rather than raising, so a bad row can't break the whole listing."""
    result = await db.scalars(select(Provider))
    providers = list(result)

    registry = get_adapter_registry()
    return [
        ProviderOut(
            id=p.id,
            name=p.name,
            adapter_key=p.adapter_key,
            status=p.status,
            requires_otp=getattr(registry.get(p.adapter_key), "requires_otp", False),
        )
        for p in providers
    ]


@router.post("", status_code=201)
async def create_provider(
    body: ProviderCreate,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """A provider row on its own doesn't make a rail usable — adapter_key has
    to match an entry actually registered in app/adapters/registry.py, or
    every transaction attempted against it fails with 'No adapter registered'.
    Rejecting an unregistered adapter_key here means that failure shows up
    immediately, as a clear error on creation, instead of confusingly later
    at transaction time. Wiring a *new* adapter_key still requires writing
    the adapter class and registering it in code first — this endpoint only
    covers the database side of an already-integrated provider."""
    existing = await db.scalar(select(Provider).where(Provider.name == body.name))
    if existing is not None:
        raise HTTPException(status_code=409, detail="A provider with this name already exists")

    existing_key = await db.scalar(select(Provider).where(Provider.adapter_key == body.adapter_key))
    if existing_key is not None:
        raise HTTPException(status_code=409, detail="A provider with this adapter_key already exists")

    if body.adapter_key not in get_adapter_registry():
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{body.adapter_key}' has no adapter registered in app/adapters/registry.py. "
                "Add and register an adapter for it first — creating the provider row alone "
                "won't let merchants actually transact through it."
            ),
        )

    provider = Provider(name=body.name, adapter_key=body.adapter_key, status=ProviderStatus.ACTIVE)
    db.add(provider)
    await db.flush()

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="provider.created",
        target_type="provider",
        target_id=str(provider.id),
        details={"name": provider.name, "adapter_key": provider.adapter_key},
    )

    await db.commit()
    await db.refresh(provider)
    return provider


@router.patch("/{provider_id}/status")
async def set_provider_status(
    provider_id: uuid.UUID,
    status: ProviderStatus,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Kill switch for a specific provider — e.g. set EcoCash to 'disabled' during
    a known outage so it disappears from every merchant's POS dropdown instantly
    rather than merchants hitting failures one at a time."""
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    previous_status = provider.status.value
    provider.status = status

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="provider.status_changed",
        target_type="provider",
        target_id=str(provider.id),
        details={"from": previous_status, "to": status.value},
    )

    await db.commit()
    return provider


@router.get("/{provider_id}/commission-rate", response_model=CommissionRateOut | None)
async def get_current_commission_rate(
    provider_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """The rate currently applied to every newly-confirmed transaction on this
    provider. None means no rate has ever been configured yet."""
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    return await commission_service.get_current_rate(db, provider_id)


@router.get("/{provider_id}/commission-rates", response_model=list[CommissionRateOut])
async def list_commission_rate_history(
    provider_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Full history, newest first — useful for reconciling what rate applied
    to a transaction confirmed on any given past date."""
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    rates = await db.scalars(
        select(ProviderCommissionRate)
        .where(ProviderCommissionRate.provider_id == provider_id)
        .order_by(ProviderCommissionRate.effective_from.desc())
    )
    return list(rates)


@router.put("/{provider_id}/commission-rate", response_model=CommissionRateOut, status_code=201)
async def set_commission_rate(
    provider_id: uuid.UUID,
    body: CommissionRateCreate,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Sets a new commission rate effective immediately. Does not touch any
    already-confirmed transaction — those keep the commission_amount they
    snapshotted under whatever rate was active when they were confirmed."""
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    new_rate = await commission_service.set_commission_rate(
        db,
        provider_id=provider_id,
        commission_type=body.commission_type,
        percentage=body.percentage,
        flat_fee=body.flat_fee,
        set_by=user.id,
    )

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="commission_rate.set",
        target_type="provider",
        target_id=str(provider_id),
        details={
            "commission_type": body.commission_type.value,
            "percentage": str(body.percentage),
            "flat_fee": str(body.flat_fee),
        },
    )

    await db.commit()
    await db.refresh(new_rate)
    return new_rate


@router.get("/{provider_id}/commissions/summary", response_model=CommissionSummaryOut)
async def get_commission_summary(
    provider_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Total commission earned from this provider, summed from the
    CommissionEntry ledger (the authoritative record — not the denormalized
    Transaction.commission_amount column) so a future REVERSED or ADJUSTED
    entry is correctly netted in, not just the original EARNED amount."""
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(CommissionEntry.amount), 0),
                func.count(func.distinct(CommissionEntry.transaction_id)),
            ).where(CommissionEntry.provider_id == provider_id)
        )
    ).one()
    total_commission, transaction_count = row

    return CommissionSummaryOut(
        provider_id=provider_id,
        provider_name=provider.name,
        total_commission=total_commission,
        transaction_count=transaction_count,
    )


@router.get("/{provider_id}/commission-entries", response_model=list[CommissionEntryOut])
async def list_commission_entries(
    provider_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """The full ledger for this provider, newest first — each row traceable
    back to its transaction_id. This is what you'd cross-reference line by
    line against a provider's own monthly commission statement: match each
    entry's transaction_id and amount against what they say they paid, and
    where they disagree, that's what provider_reported_amount and
    reconciled_at (currently unset on every entry — nothing populates them
    yet) exist to eventually record."""
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    entries = await db.scalars(
        select(CommissionEntry)
        .where(CommissionEntry.provider_id == provider_id)
        .order_by(CommissionEntry.created_at.desc())
    )
    return list(entries)
