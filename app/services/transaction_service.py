import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import NormalizedStatus
from app.adapters.registry import get_adapter
from app.config import get_settings
from app.models import (
    CallbackRaw,
    Merchant,
    MerchantProviderAccount,
    MerchantStatus,
    Provider,
    ProviderStatus,
    Receipt,
    Shop,
    ShopStatus,
    Till,
    TillStatus,
    Transaction,
    TransactionEvent,
    TransactionStatus,
    TransactionType,
)
from app.services import commission_service

settings = get_settings()

_STATUS_MAP = {
    NormalizedStatus.PENDING: TransactionStatus.PENDING_CONFIRMATION,
    NormalizedStatus.CONFIRMED: TransactionStatus.CONFIRMED,
    NormalizedStatus.DECLINED: TransactionStatus.DECLINED,
    NormalizedStatus.FAILED: TransactionStatus.FAILED,
}


class DuplicateTransactionError(Exception):
    pass


class ProviderUnavailableError(Exception):
    pass


class TillBlockedError(Exception):
    pass

class OtpConfirmationError(Exception):
    pass


class MerchantNotActiveError(Exception):
    pass


async def _record_event(
    db: AsyncSession, txn: Transaction, to_status: TransactionStatus, actor: str, note: str | None = None
) -> None:
    db.add(
        TransactionEvent(
            transaction_id=txn.id,
            from_status=txn.status.value if txn.status else None,
            to_status=to_status.value,
            actor=actor,
            note=note,
        )
    )


async def _apply_commission(db: AsyncSession, txn: Transaction) -> None:
    """Snapshots the commission PayPulse earns on this transaction, using
    whichever ProviderCommissionRate is active right now. Called exactly once,
    at the moment a transaction first becomes CONFIRMED — never recalculated
    afterward, even if the provider's rate changes later. A provider with no
    rate configured yet just earns nothing recorded (None), not an error.

    Writes to two places: the CommissionEntry ledger (the authoritative,
    audit/reconciliation record — traceable back to this exact transaction_id,
    and where a later correction or provider-statement reconciliation gets
    recorded) and Transaction.commission_amount/commission_rate_id (a
    denormalized copy for fast reads, e.g. listing transactions without a
    join). The ledger entry is the source of truth if the two ever disagree."""
    rate = await commission_service.get_current_rate(db, txn.provider_id)
    if rate is None:
        return
    amount = commission_service.commission_for(rate, txn.amount)
    if amount is None:  # tiered rate with no band covering this amount
        return
    await commission_service.record_commission_entry(
        db,
        transaction_id=txn.id,
        provider_id=txn.provider_id,
        commission_rate_id=rate.id,
        amount=amount,
    )
    txn.commission_rate_id = rate.id
    txn.commission_amount = amount


async def initiate_transaction(
    db: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    initiated_by: uuid.UUID,
    provider_adapter_key: str,
    merchant_provider_account_id: uuid.UUID,
    customer_msisdn: str,
    amount: Decimal,
    txn_type: TransactionType,
    idempotency_key: str,
    device_id: str | None,
    shop_id: uuid.UUID | None = None,
) -> Transaction:
    """Create the transaction row, then hand off to the provider adapter. Every
    step that changes status also writes a TransactionEvent, so the full history
    survives even though `transactions` itself is mutated in place."""

    # Idempotency check first — a retried request should return the existing
    # transaction rather than create a duplicate.
    existing = await db.scalar(select(Transaction).where(Transaction.idempotency_key == idempotency_key))
    if existing is not None:
        return existing

    provider = await db.scalar(select(Provider).where(Provider.adapter_key == provider_adapter_key))
    if provider is None:
        raise ValueError(f"Unknown provider '{provider_adapter_key}'")
    if provider.status != ProviderStatus.ACTIVE:
        raise ProviderUnavailableError(f"Provider '{provider.name}' is currently {provider.status.value}")

    account = await db.get(MerchantProviderAccount, merchant_provider_account_id)
    if account is None or account.merchant_id != merchant_id or not account.is_active:
        raise ValueError("Invalid or inactive merchant provider account")

    # Suspending a merchant or a shop only means something if it actually
    # stops money moving. Until this check existed, a suspended merchant's
    # tellers could carry on transacting; the status was a label, nothing more.
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None or merchant.status == MerchantStatus.PENDING_KYC:
        raise MerchantNotActiveError("This merchant hasn't completed verification yet")
    if merchant.status != MerchantStatus.ACTIVE:
        raise MerchantNotActiveError("This merchant account is suspended. Contact PayPulse support")
    # Which shop made the sale comes from the device / teller it was made on
    # (provider accounts are merchant-wide), not from the account. An account
    # pinned to one shop still can't be used from another.
    if account.shop_id is not None and shop_id is not None and account.shop_id != shop_id:
        raise ValueError("This provider account belongs to a different shop")
    effective_shop_id = shop_id or account.shop_id
    if effective_shop_id is not None:
        shop = await db.get(Shop, effective_shop_id)
        if shop is not None and shop.status != ShopStatus.ACTIVE:
            raise MerchantNotActiveError("This shop is suspended. Contact your manager")

    # device_id here is whatever the POS client sends — matched against the
    # Till registry (a merchant's registered checkout terminals) if it
    # corresponds to one. Deliberately soft: a device_id with no matching
    # Till row is still allowed through, so an un-migrated or ad-hoc client
    # isn't locked out. Only a device_id that DOES match a till, where that
    # till has been explicitly blocked, is rejected — this is the actual
    # fraud-response mechanism: blocking a till here is what stops it.
    if device_id:
        till = await db.scalar(
            select(Till).where(Till.merchant_id == merchant_id, Till.till_identifier == device_id)
        )
        if till is not None and till.status == TillStatus.BLOCKED:
            raise TillBlockedError(f"This till has been blocked: {till.blocked_reason or 'no reason given'}")

    txn = Transaction(
        merchant_id=merchant_id,
        shop_id=effective_shop_id,
        provider_id=provider.id,
        merchant_provider_account_id=account.id,
        initiated_by=initiated_by,
        type=txn_type,
        status=TransactionStatus.INITIATED,
        customer_msisdn=customer_msisdn,
        amount=amount,
        idempotency_key=idempotency_key,
        device_id=device_id,
    )
    db.add(txn)
    try:
        await db.flush()  # get txn.id, and surface a duplicate idempotency_key race
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(select(Transaction).where(Transaction.idempotency_key == idempotency_key))
        if existing:
            return existing
        raise DuplicateTransactionError(idempotency_key)

    # A just-created row has no relationships loaded; the response includes
    # shop_name, so load it now (see Transaction.shop_name).
    await db.refresh(txn, attribute_names=["shop"])

    await _record_event(db, txn, TransactionStatus.INITIATED, actor=str(initiated_by))

    adapter = get_adapter(provider_adapter_key)
    txn_ref = str(txn.id)

    try:
        if txn_type == TransactionType.COLLECTION:
            response = await adapter.initiate_collection(
                account_identifier=account.account_identifier,
                customer_msisdn=customer_msisdn,
                amount=amount,
                txn_ref=txn_ref,
            )
        else:
            response = await adapter.initiate_withdrawal(
                account_identifier=account.account_identifier, amount=amount, txn_ref=txn_ref
            )
    except Exception as exc:  # provider call failed before it even accepted the request
        txn.status = TransactionStatus.FAILED
        txn.decline_reason = f"Provider request failed: {exc}"
        await _record_event(db, txn, TransactionStatus.FAILED, actor="system", note=str(exc))
        await db.commit()
        return txn

    txn.provider_reference = response.provider_reference
    txn.status = _STATUS_MAP.get(response.status, TransactionStatus.SENT_TO_PROVIDER)
    await _record_event(db, txn, txn.status, actor="system", note="provider accepted request")
    await db.commit()
    return txn

async def confirm_collection_otp(db: AsyncSession, *, transaction_id: uuid.UUID, otp: str) -> Transaction:
    """Second step for a provider adapter with requires_otp=True (currently
    only C-Pay): the customer already received an OTP out-of-band (SMS) after
    initiate_transaction()'s call to adapter.initiate_collection(), and the
    teller is now entering it into the POS.

    Unlike process_callback(), this resolves the transaction SYNCHRONOUSLY —
    C-Pay's /confirm response IS the final outcome, there is no separate
    webhook to wait for. The router endpoint calling this (e.g.
    POST /transactions/{id}/confirm-otp) can return the updated transaction
    directly to the POS app in the same request/response cycle, rather than
    the POS having to poll for a status change the way it does after the
    initial initiate call.

    Idempotent: a transaction already in a terminal state is returned as-is
    rather than re-confirmed (e.g. a retried request after the POS timed out
    waiting for a slow response, but the first attempt actually went through).
    """
    txn = await db.get(Transaction, transaction_id)
    if txn is None:
        raise ValueError("Transaction not found")

    terminal_states = {
        TransactionStatus.CONFIRMED,
        TransactionStatus.DECLINED,
        TransactionStatus.EXPIRED,
        TransactionStatus.FAILED,
    }
    if txn.status in terminal_states:
        return txn

    if txn.status not in (TransactionStatus.SENT_TO_PROVIDER, TransactionStatus.PENDING_CONFIRMATION):
        raise OtpConfirmationError(f"Transaction is in state {txn.status.value}, not awaiting OTP confirmation")

    provider = await db.get(Provider, txn.provider_id)
    adapter = get_adapter(provider.adapter_key)

    if not getattr(adapter, "requires_otp", False):
        raise OtpConfirmationError(f"Provider '{provider.name}' does not use OTP confirmation")

    try:
        response = await adapter.confirm_collection(
            provider_reference=txn.provider_reference,
            otp=otp,
            customer_msisdn=txn.customer_msisdn,
            amount=txn.amount,
            txn_ref=str(txn.id),
        )
    except Exception as exc:
        txn.status = TransactionStatus.FAILED
        txn.decline_reason = f"OTP confirmation request failed: {exc}"
        await _record_event(db, txn, TransactionStatus.FAILED, actor="system", note=str(exc))
        await db.commit()
        return txn

    new_status = _STATUS_MAP.get(response.status, TransactionStatus.FAILED)
    txn.status = new_status
    if new_status == TransactionStatus.CONFIRMED:
        txn.confirmed_at = datetime.now(timezone.utc)
        await _apply_commission(db, txn)
    if new_status in (TransactionStatus.DECLINED, TransactionStatus.FAILED):
        txn.decline_reason = (
            response.raw.get("Description") or response.raw.get("ReasonCode") or "Declined by provider"
        )

    await _record_event(db, txn, new_status, actor="otp_confirmation", note=str(response.raw))
    await db.commit()
    return txn



async def process_callback(
    db: AsyncSession, *, provider_adapter_key: str, provider_id: uuid.UUID, raw_payload: dict, payload_bytes: bytes, headers: dict
) -> Transaction | None:
    """Entry point for the /callbacks/{provider} endpoint. Always logs the raw
    payload first — even if signature verification fails — so nothing inbound is
    ever silently dropped without a trace."""

    adapter = get_adapter(provider_adapter_key)
    signature_valid = adapter.verify_callback_signature(payload_bytes, headers)

    callback_row = CallbackRaw(
        provider_id=provider_id, payload=raw_payload, signature_valid=signature_valid, processed=False
    )
    db.add(callback_row)
    await db.flush()

    if not signature_valid:
        await db.commit()
        return None  # rejected — logged for forensics, nothing else happens

    result = adapter.parse_callback(raw_payload)

    txn = await db.scalar(select(Transaction).where(Transaction.provider_reference == result.provider_reference))
    if txn is None:
        callback_row.processed = True  # nothing to apply it to; still mark seen
        await db.commit()
        return None

    # Callback idempotency: a transaction already in a terminal state ignores
    # duplicate callbacks rather than re-applying them.
    terminal_states = {
        TransactionStatus.CONFIRMED,
        TransactionStatus.DECLINED,
        TransactionStatus.EXPIRED,
        TransactionStatus.FAILED,
    }
    if txn.status in terminal_states:
        callback_row.processed = True
        await db.commit()
        return txn

    new_status = _STATUS_MAP.get(result.status, TransactionStatus.FAILED)
    txn.status = new_status
    if new_status == TransactionStatus.CONFIRMED:
        txn.confirmed_at = datetime.now(timezone.utc)
        await _apply_commission(db, txn)
    if new_status in (TransactionStatus.DECLINED, TransactionStatus.FAILED):
        txn.decline_reason = result.reason or "Declined by provider"

    await _record_event(db, txn, new_status, actor="callback", note=result.reason)
    callback_row.processed = True
    await db.commit()
    return txn


async def issue_receipt(db: AsyncSession, txn: Transaction) -> Receipt:
    if txn.status != TransactionStatus.CONFIRMED:
        raise ValueError("Cannot issue a receipt for a transaction that is not confirmed")

    existing = await db.scalar(select(Receipt).where(Receipt.transaction_id == txn.id))
    if existing:
        existing.reprint_count += 1
        await db.commit()
        return existing

    receipt_number = f"RCT-{txn.id.hex[:12].upper()}"
    receipt = Receipt(transaction_id=txn.id, receipt_number=receipt_number)
    db.add(receipt)
    await db.commit()
    return receipt


async def expire_stale_transactions(db: AsyncSession) -> int:
    """Run periodically (Celery beat) to sweep transactions that never received a
    callback. Called out separately from the request path since nothing about
    this depends on any single HTTP request being in flight."""

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.transaction_timeout_minutes)
    stale = await db.scalars(
        select(Transaction).where(
            Transaction.status.in_(
                [TransactionStatus.SENT_TO_PROVIDER, TransactionStatus.PENDING_CONFIRMATION]
            ),
            Transaction.created_at < cutoff,
        )
    )
    count = 0
    for txn in stale:
        txn.status = TransactionStatus.EXPIRED
        txn.decline_reason = "No provider confirmation received before timeout"
        await _record_event(db, txn, TransactionStatus.EXPIRED, actor="system", note="timeout sweep")
        count += 1
    await db.commit()
    return count
