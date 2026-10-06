import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentUser, get_current_user, require_roles
from app.models import CommissionEntry, Transaction, TransactionStatus, TransactionType
from app.schemas import CommissionEntryOut, ReceiptOut, TransactionCreate, TransactionOut, WithdrawalCreate
from app.services import transaction_service

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.post("", response_model=TransactionOut, status_code=201)
async def create_transaction(
    body: TransactionCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.merchant_id is None:
        raise HTTPException(status_code=403, detail="Only merchant users can initiate transactions")

    try:
        txn = await transaction_service.initiate_transaction(
            db,
            merchant_id=user.merchant_id,
            initiated_by=user.id,
            provider_adapter_key=body.provider_adapter_key,
            merchant_provider_account_id=body.merchant_provider_account_id,
            customer_msisdn=body.customer_msisdn,
            amount=body.amount,
            txn_type=body.type,
            idempotency_key=body.idempotency_key,
            device_id=body.device_id,
        )
    except transaction_service.ProviderUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (transaction_service.TillBlockedError, transaction_service.MerchantNotActiveError) as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return txn


class OtpConfirmRequest(BaseModel):
    otp: str


@router.post("/{transaction_id}/confirm-otp", response_model=TransactionOut)
async def confirm_transaction_otp(
    transaction_id: uuid.UUID,
    body: OtpConfirmRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Second step for a provider adapter with requires_otp=True (currently
    only C-Pay). After create_transaction() returns a PENDING transaction for
    such a provider, the teller asks the customer for the OTP they received
    by SMS and posts it here. Unlike every other confirmation path in this
    app, this resolves synchronously — the response IS the final outcome,
    there is no callback to wait for afterward."""
    txn = await db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if user.role != "platform_admin" and txn.merchant_id != user.merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this transaction")

    try:
        txn = await transaction_service.confirm_collection_otp(db, transaction_id=transaction_id, otp=body.otp)
    except transaction_service.OtpConfirmationError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return txn


@router.post("/withdrawals", response_model=TransactionOut, status_code=201)
async def create_withdrawal(
    body: WithdrawalCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.merchant_id is None:
        raise HTTPException(status_code=403, detail="Only merchant users can initiate withdrawals")

    # Withdrawals above the configured threshold should route through an approval
    # step (a second user confirming) rather than executing immediately — wire
    # that check in here once the approvals table/flow exists.

    try:
        txn = await transaction_service.initiate_transaction(
            db,
            merchant_id=user.merchant_id,
            initiated_by=user.id,
            provider_adapter_key=body.provider_adapter_key,
            merchant_provider_account_id=body.merchant_provider_account_id,
            customer_msisdn="",  # withdrawals move to the merchant's own account, not a customer
            amount=body.amount,
            txn_type=TransactionType.WITHDRAWAL,
            idempotency_key=body.idempotency_key,
            device_id=None,
        )
    except transaction_service.ProviderUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (transaction_service.TillBlockedError, transaction_service.MerchantNotActiveError) as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return txn


@router.get("/{transaction_id}", response_model=TransactionOut)
async def get_transaction(
    transaction_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    txn = await db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if user.role != "platform_admin" and txn.merchant_id != user.merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this transaction")
    return txn


@router.get("", response_model=list[TransactionOut])
async def list_transactions(
    provider_id: uuid.UUID | None = Query(None),
    merchant_id: uuid.UUID | None = Query(None),
    shop_id: uuid.UUID | None = Query(None),
    initiated_by: uuid.UUID | None = Query(None),
    search: str | None = Query(None, description="Matches against customer MSISDN or provider reference"),
    status: TransactionStatus | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(100, le=500),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Scoping differs by role, not just by a shared merchant_id filter:
    - platform_admin: global, every filter available, including merchant_id.
    - merchant_owner (and compliance_officer): everything for their own
      merchant, filterable by shop_id/initiated_by/search — this is the
      "which shop, which teller" reporting view.
    - teller: forced to their own initiated_by AND their own merchant,
      regardless of what they pass. A teller has no legitimate reason to see
      a colleague's till activity; that's the owner's or an admin's job."""
    query = select(Transaction).order_by(Transaction.created_at.desc()).limit(limit)

    if user.role == "platform_admin":
        if merchant_id is not None:
            query = query.where(Transaction.merchant_id == merchant_id)
        if shop_id is not None:
            query = query.where(Transaction.shop_id == shop_id)
        if initiated_by is not None:
            query = query.where(Transaction.initiated_by == initiated_by)
    elif user.role == "teller":
        query = query.where(Transaction.merchant_id == user.merchant_id, Transaction.initiated_by == user.id)
    else:
        query = query.where(Transaction.merchant_id == user.merchant_id)
        if shop_id is not None:
            query = query.where(Transaction.shop_id == shop_id)
        if initiated_by is not None:
            query = query.where(Transaction.initiated_by == initiated_by)

    if provider_id is not None:
        query = query.where(Transaction.provider_id == provider_id)
    if status is not None:
        query = query.where(Transaction.status == status)
    if date_from is not None:
        query = query.where(Transaction.created_at >= date_from)
    if date_to is not None:
        query = query.where(Transaction.created_at <= date_to)
    if search:
        like = f"%{search}%"
        query = query.where(
            (Transaction.customer_msisdn.ilike(like)) | (Transaction.provider_reference.ilike(like))
        )

    result = await db.scalars(query)
    return list(result)


@router.post("/{transaction_id}/receipt", response_model=ReceiptOut)
async def print_receipt(
    transaction_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    txn = await db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if user.role != "platform_admin" and txn.merchant_id != user.merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this transaction")

    try:
        receipt = await transaction_service.issue_receipt(db, txn)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return ReceiptOut(receipt_number=receipt.receipt_number, transaction=txn)


@router.get("/{transaction_id}/commission-entries", response_model=list[CommissionEntryOut])
async def get_transaction_commission_entries(
    transaction_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Look up exactly what was recorded in the commission ledger for one
    specific transaction — normally a single EARNED entry, but returned as a
    list since a REVERSED or ADJUSTED entry against the same transaction_id
    would show up here too, alongside the original. platform_admin only:
    commission figures are PayPulse's own margin, not something the merchant
    who owns this transaction has any visibility into."""
    txn = await db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    entries = await db.scalars(
        select(CommissionEntry)
        .where(CommissionEntry.transaction_id == transaction_id)
        .order_by(CommissionEntry.created_at.asc())
    )
    return list(entries)
