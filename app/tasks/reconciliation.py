import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.adapters.base import NormalizedStatus
from app.adapters.registry import get_adapter
from app.database import AsyncSessionLocal
from app.models import (
    DailySettlement,
    MerchantProviderAccount,
    Provider,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from app.services.transaction_service import _STATUS_MAP, _apply_commission, _record_event
from app.tasks.celery_app import celery_app


async def _poll_pending() -> int:
    """Directly asks each provider for the status of anything still pending,
    to catch cases where the callback itself never arrived. This is the
    reconciliation safety net described alongside the callback-trust design."""
    checked = 0
    async with AsyncSessionLocal() as db:
        pending = await db.scalars(
            select(Transaction).where(
                Transaction.status.in_(
                    [TransactionStatus.SENT_TO_PROVIDER, TransactionStatus.PENDING_CONFIRMATION]
                )
            )
        )
        for txn in pending:
            provider = await db.get(Provider, txn.provider_id)
            adapter = get_adapter(provider.adapter_key)
            if not txn.provider_reference:
                continue
            status = await adapter.check_status(txn.provider_reference)
            new_status = _STATUS_MAP.get(status, txn.status)
            if new_status != txn.status:
                await _record_event(db, txn, new_status, actor="reconciliation_poll")
                txn.status = new_status
                if new_status == TransactionStatus.CONFIRMED:
                    txn.confirmed_at = datetime.now(timezone.utc)
                    await _apply_commission(db, txn)
            checked += 1
        await db.commit()
    return checked


async def _run_daily_settlement() -> int:
    """Locks in an immutable per-merchant, per-provider snapshot for the day:
    opening balance, totals, and a discrepancy against the provider's own
    reported balance. Meant to run once, near end of day, not recomputed live."""

    written = 0
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    async with AsyncSessionLocal() as db:
        accounts = await db.scalars(select(MerchantProviderAccount))
        for account in accounts:
            provider = await db.get(Provider, account.provider_id)
            adapter = get_adapter(provider.adapter_key)

            todays_txns = await db.scalars(
                select(Transaction).where(
                    Transaction.merchant_provider_account_id == account.id,
                    Transaction.status == TransactionStatus.CONFIRMED,
                    Transaction.confirmed_at >= day_start,
                    Transaction.confirmed_at < day_end,
                )
            )
            total_collections = Decimal("0")
            total_withdrawals = Decimal("0")
            for t in todays_txns:
                if t.type == TransactionType.COLLECTION:
                    total_collections += t.amount
                else:
                    total_withdrawals += t.amount

            opening_balance = account.cached_balance or Decimal("0")
            computed_closing = opening_balance + total_collections - total_withdrawals

            provider_balance = await adapter.get_balance(account.account_identifier)
            discrepancy = provider_balance.amount - computed_closing

            db.add(
                DailySettlement(
                    merchant_id=account.merchant_id,
                    shop_id=account.shop_id,
                    provider_id=account.provider_id,
                    merchant_provider_account_id=account.id,
                    settlement_date=day_start,
                    opening_balance=opening_balance,
                    total_collections=total_collections,
                    total_withdrawals=total_withdrawals,
                    provider_reported_closing_balance=provider_balance.amount,
                    computed_closing_balance=computed_closing,
                    discrepancy=discrepancy,
                )
            )
            # Next day's opening balance starts from what the provider actually reports.
            account.cached_balance = provider_balance.amount
            account.balance_updated_at = provider_balance.as_of
            written += 1
        await db.commit()
    return written


@celery_app.task(name="app.tasks.reconciliation.poll_pending_transactions_task")
def poll_pending_transactions_task() -> int:
    return asyncio.run(_poll_pending())


@celery_app.task(name="app.tasks.reconciliation.run_daily_settlement_task")
def run_daily_settlement_task() -> int:
    return asyncio.run(_run_daily_settlement())
