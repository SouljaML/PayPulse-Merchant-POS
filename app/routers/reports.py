"""Admin reporting: how much money has moved through PayPulse, and what it
earned in commission.

Every figure here comes from one query over CONFIRMED transactions in the
period, aggregated in a single pass — so the totals, the per-provider rows,
the per-merchant rows and the daily bars can never disagree with each other.
That's the right trade at today's volumes. The day it isn't (hundreds of
thousands of confirmed transactions in a range), move the aggregation into SQL
or a nightly summary table, the same way daily_settlements works.

Two deliberate choices about what a number means:

- "Moved" counts confirmed transactions only, by the day they were CONFIRMED.
  A payment initiated on the 30th and confirmed on the 1st belongs to the 1st,
  which is also when its commission was earned.
- A "day" is a local day (settings.report_timezone), not a UTC day.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import CurrentUser, require_roles
from app.models import (
    CommissionEntry,
    CommissionType,
    Merchant,
    Provider,
    ProviderCommissionRate,
    Transaction,
    TransactionStatus,
    TransactionType,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["reports"])

ZERO = Decimal("0.00")
MAX_RANGE_DAYS = 366
CURRENCY = "LSL"  # single-currency today; every amount below is in this


class MoneyStats(BaseModel):
    confirmed_count: int
    volume: Decimal
    collections_volume: Decimal
    withdrawals_volume: Decimal
    commission: Decimal
    # Confirmed transactions that earned no commission — normally because no
    # rate was configured for that provider when they confirmed.
    uncommissioned_count: int


class ProviderReportRow(MoneyStats):
    provider_id: str
    provider_name: str
    current_rate: str | None


class MerchantReportRow(BaseModel):
    merchant_id: str
    merchant_name: str
    confirmed_count: int
    volume: Decimal
    commission: Decimal


class DayReportRow(BaseModel):
    day: date
    confirmed_count: int
    volume: Decimal
    commission: Decimal


class Outcomes(BaseModel):
    """Every attempt CREATED in the period, whatever became of it."""

    confirmed: int
    declined: int
    failed: int
    expired: int
    pending: int


class ReportSummary(BaseModel):
    date_from: date
    date_to: date
    timezone: str
    currency: str
    totals: MoneyStats
    by_provider: list[ProviderReportRow]
    by_merchant: list[MerchantReportRow]
    by_day: list[DayReportRow]
    outcomes: Outcomes


class _Acc:
    """Running totals for one bucket (a provider, a merchant, a day, or all)."""

    __slots__ = ("count", "collections", "withdrawals", "commission", "uncommissioned")

    def __init__(self) -> None:
        self.count = 0
        self.collections = ZERO
        self.withdrawals = ZERO
        self.commission = ZERO
        self.uncommissioned = 0

    def add(self, txn_type: TransactionType, amount: Decimal, commission: Decimal | None) -> None:
        self.count += 1
        if txn_type == TransactionType.WITHDRAWAL:
            self.withdrawals += amount
        else:
            self.collections += amount
        if commission is None:
            self.uncommissioned += 1
        else:
            self.commission += commission

    @property
    def volume(self) -> Decimal:
        return self.collections + self.withdrawals

    def stats(self) -> MoneyStats:
        return MoneyStats(
            confirmed_count=self.count,
            volume=self.volume,
            collections_volume=self.collections,
            withdrawals_volume=self.withdrawals,
            commission=self.commission,
            uncommissioned_count=self.uncommissioned,
        )


def _report_tz() -> tuple[tzinfo, str]:
    name = get_settings().report_timezone
    try:
        return ZoneInfo(name), name
    except ZoneInfoNotFoundError:
        # No tz database on this machine (some Windows installs). Lesotho has
        # no daylight saving, so a fixed +02:00 is exactly right for it.
        logger.warning("timezone %r not found on this system; using fixed UTC+02:00", name)
        return timezone(timedelta(hours=2)), "UTC+02:00"


def _local_day(ts: datetime, tz: tzinfo) -> date:
    if ts.tzinfo is None:  # SQLite hands back naive timestamps; they are UTC
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(tz).date()


def _rate_label(rate: ProviderCommissionRate) -> str:
    pct = f"{(rate.percentage * 100).quantize(Decimal('0.01'))}%"
    flat = f"{CURRENCY} {rate.flat_fee.quantize(Decimal('0.01'))}"
    if rate.commission_type == CommissionType.PERCENTAGE:
        return pct
    if rate.commission_type == CommissionType.FLAT:
        return flat
    return f"{pct} + {flat}"


@router.get("/summary", response_model=ReportSummary)
async def report_summary(
    date_from: date | None = Query(None, description="First local day, inclusive. Defaults to the 1st of date_to's month."),
    date_to: date | None = Query(None, description="Last local day, inclusive. Defaults to today."),
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """platform_admin only: commission is PayPulse's own margin, the same
    reason the per-provider commission endpoints are admin-only."""
    tz, tz_name = _report_tz()
    d_to = date_to or datetime.now(tz).date()
    d_from = date_from or d_to.replace(day=1)
    if d_from > d_to:
        raise HTTPException(status_code=400, detail="date_from can't be after date_to")
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"Choose a range of at most {MAX_RANGE_DAYS} days")

    # Local-day boundaries, expressed in UTC to compare against stored timestamps.
    start = datetime.combine(d_from, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)

    # A transaction can have several ledger entries (a reversal later, say), so
    # sum them per transaction before joining.
    commission_per_txn = (
        select(CommissionEntry.transaction_id.label("tid"), func.sum(CommissionEntry.amount).label("commission"))
        .group_by(CommissionEntry.transaction_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(
                Transaction.provider_id,
                Transaction.merchant_id,
                Transaction.type,
                Transaction.amount,
                Transaction.confirmed_at,
                commission_per_txn.c.commission,
            )
            .outerjoin(commission_per_txn, commission_per_txn.c.tid == Transaction.id)
            .where(
                Transaction.status == TransactionStatus.CONFIRMED,
                Transaction.confirmed_at >= start,
                Transaction.confirmed_at < end,
            )
        )
    ).all()

    total = _Acc()
    per_provider: dict = {}
    per_merchant: dict = {}
    per_day: dict[date, _Acc] = {}
    for provider_id, merchant_id, txn_type, amount, confirmed_at, commission in rows:
        total.add(txn_type, amount, commission)
        per_provider.setdefault(provider_id, _Acc()).add(txn_type, amount, commission)
        per_merchant.setdefault(merchant_id, _Acc()).add(txn_type, amount, commission)
        per_day.setdefault(_local_day(confirmed_at, tz), _Acc()).add(txn_type, amount, commission)

    # Every provider gets a row, including ones with no activity — an admin
    # scanning for "why is nothing moving on EcoCash" needs to see the zero.
    providers = (await db.scalars(select(Provider).order_by(Provider.name))).all()
    current_rates = {
        r.provider_id: r
        for r in await db.scalars(select(ProviderCommissionRate).where(ProviderCommissionRate.effective_to.is_(None)))
    }
    by_provider = [
        ProviderReportRow(
            provider_id=str(p.id),
            provider_name=p.name,
            current_rate=_rate_label(current_rates[p.id]) if p.id in current_rates else None,
            **(per_provider.get(p.id) or _Acc()).stats().model_dump(),
        )
        for p in providers
    ]

    merchant_names = {
        m.id: m.trading_name for m in await db.scalars(select(Merchant).where(Merchant.id.in_(list(per_merchant))))
    }
    by_merchant = sorted(
        (
            MerchantReportRow(
                merchant_id=str(mid),
                merchant_name=merchant_names.get(mid, "Unknown"),
                confirmed_count=acc.count,
                volume=acc.volume,
                commission=acc.commission,
            )
            for mid, acc in per_merchant.items()
        ),
        key=lambda r: r.volume,
        reverse=True,
    )

    by_day = []
    day = d_from
    while day <= d_to:
        acc = per_day.get(day) or _Acc()
        by_day.append(DayReportRow(day=day, confirmed_count=acc.count, volume=acc.volume, commission=acc.commission))
        day += timedelta(days=1)

    # Attempts are counted by when they were created, since a declined or
    # expired attempt never has a confirmation date.
    status_counts = dict(
        (
            await db.execute(
                select(Transaction.status, func.count())
                .where(Transaction.created_at >= start, Transaction.created_at < end)
                .group_by(Transaction.status)
            )
        ).all()
    )
    count = lambda *statuses: sum(status_counts.get(s, 0) for s in statuses)  # noqa: E731
    outcomes = Outcomes(
        confirmed=count(TransactionStatus.CONFIRMED),
        declined=count(TransactionStatus.DECLINED),
        failed=count(TransactionStatus.FAILED),
        expired=count(TransactionStatus.EXPIRED),
        pending=count(
            TransactionStatus.INITIATED, TransactionStatus.SENT_TO_PROVIDER, TransactionStatus.PENDING_CONFIRMATION
        ),
    )

    return ReportSummary(
        date_from=d_from,
        date_to=d_to,
        timezone=tz_name,
        currency=CURRENCY,
        totals=total.stats(),
        by_provider=by_provider,
        by_merchant=by_merchant,
        by_day=by_day,
        outcomes=outcomes,
    )
