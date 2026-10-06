import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class TransactionType(str, enum.Enum):
    COLLECTION = "collection"   # merchant collects money from a customer
    WITHDRAWAL = "withdrawal"   # merchant withdraws from a provider account


class TransactionStatus(str, enum.Enum):
    INITIATED = "initiated"
    SENT_TO_PROVIDER = "sent_to_provider"
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    EXPIRED = "expired"
    FAILED = "failed"


class ProviderStatus(str, enum.Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class MerchantStatus(str, enum.Enum):
    PENDING_KYC = "pending_kyc"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class KycDocStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    REJECTED = "rejected"


class CommissionType(str, enum.Enum):
    PERCENTAGE = "percentage"                  # e.g. 1.5% of transaction amount
    FLAT = "flat"                               # e.g. flat LSL 2.00 per transaction
    PERCENTAGE_PLUS_FLAT = "percentage_plus_flat"  # both combined


# --------------------------------------------------------------------------
# Identity / merchants
# --------------------------------------------------------------------------

class ShopStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class TillStatus(str, enum.Enum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    legal_name: Mapped[str] = mapped_column(String(200))
    trading_name: Mapped[str] = mapped_column(String(200))
    registration_number: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[MerchantStatus] = mapped_column(
        Enum(MerchantStatus, name="merchant_status"), default=MerchantStatus.PENDING_KYC
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    users: Mapped[list["User"]] = relationship(back_populates="merchant")
    provider_accounts: Mapped[list["MerchantProviderAccount"]] = relationship(back_populates="merchant")
    kyc_documents: Mapped[list["MerchantKycDocument"]] = relationship(back_populates="merchant")
    shops: Mapped[list["Shop"]] = relationship(back_populates="merchant")


class Shop(Base):
    """A physical branch/till location under a merchant. Each shop has its
    own provider accounts (tills) — see MerchantProviderAccount.shop_id — so
    balances and settlements are genuinely per-shop, not just a reporting
    label on top of one shared merchant-wide till."""

    __tablename__ = "shops"
    __table_args__ = (UniqueConstraint("merchant_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    name: Mapped[str] = mapped_column(String(150))
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[ShopStatus] = mapped_column(Enum(ShopStatus, name="shop_status"), default=ShopStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    merchant: Mapped["Merchant"] = relationship(back_populates="shops")


class Till(Base):
    """A physical checkout terminal within a shop — not tied to any one
    provider, since PayPulse's whole point is that a teller on any till can
    process a transaction against any of the shop's provider accounts. This
    is the actual fraud-control unit: blocking a till here is what stops it
    transacting (see the till check in transaction_service) — unlike the
    free-text device_id string on Transaction, which nothing validated on
    its own before this. A transaction whose device_id doesn't match any
    registered till here is still allowed through (soft enforcement, so an
    un-migrated POS client isn't locked out entirely); only a device_id
    matching a row here with status=blocked is rejected."""

    __tablename__ = "tills"
    __table_args__ = (UniqueConstraint("merchant_id", "till_identifier"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    shop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shops.id"))
    till_identifier: Mapped[str] = mapped_column(String(150))
    label: Mapped[str] = mapped_column(String(150))
    status: Mapped[TillStatus] = mapped_column(Enum(TillStatus, name="till_status"), default=TillStatus.ACTIVE)
    blocked_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MerchantKycDocument(Base):
    __tablename__ = "merchant_kyc_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    doc_type: Mapped[str] = mapped_column(String(100))  # e.g. "business_registration", "director_id"
    file_reference: Mapped[str] = mapped_column(String(500))  # path/key in object storage, not the file itself
    status: Mapped[KycDocStatus] = mapped_column(
        Enum(KycDocStatus, name="kyc_doc_status"), default=KycDocStatus.SUBMITTED
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    merchant: Mapped["Merchant"] = relationship(back_populates="kyc_documents")


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(50), unique=True)  # e.g. "platform_admin", "merchant_owner", "teller"
    description: Mapped[str] = mapped_column(String(300), default="")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("merchants.id"), nullable=True)
    shop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shops.id"), nullable=True)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(300))
    full_name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set when someone else (an admin or a merchant owner) resets this
    # account's password: the temporary password they were handed only
    # works for changing it. Enforced per-request in get_current_user.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    mfa_secret: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    merchant: Mapped["Merchant | None"] = relationship(back_populates="users")
    role: Mapped["Role"] = relationship()


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True)          # "C-Pay", "M-Pesa", ...
    adapter_key: Mapped[str] = mapped_column(String(50), unique=True)    # "cpay", "mpesa", "ecocash", ...
    status: Mapped[ProviderStatus] = mapped_column(
        Enum(ProviderStatus, name="provider_status"), default=ProviderStatus.ACTIVE
    )


class MerchantProviderAccount(Base):
    __tablename__ = "merchant_provider_accounts"
    __table_args__ = (UniqueConstraint("merchant_id", "provider_id", "account_identifier"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    shop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shops.id"), nullable=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    account_identifier: Mapped[str] = mapped_column(String(100))  # MSISDN or provider account ID
    cached_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    balance_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    merchant: Mapped["Merchant"] = relationship(back_populates="provider_accounts")
    provider: Mapped["Provider"] = relationship()


class CommissionEntryType(str, enum.Enum):
    EARNED = "earned"       # normal commission recorded when a transaction confirms
    REVERSED = "reversed"   # offsets an EARNED entry if the underlying transaction is later reversed
    ADJUSTED = "adjusted"   # manual correction, e.g. reconciling against a provider's own statement


class CommissionEntry(Base):
    """The audit/reconciliation ledger for commissions — one row per event,
    never mutated after creation. Always traceable back to the exact
    transaction it came from via transaction_id, which is what makes this
    usable for reconciling against a provider's own commission statement.

    Transaction.commission_amount / commission_rate_id are a denormalized
    snapshot for fast reads (e.g. rendering a transaction list without a
    join); this table is the authoritative record. A transaction normally
    has exactly one EARNED entry, but the schema deliberately doesn't enforce
    that with a unique constraint — a REVERSED or ADJUSTED entry against the
    same transaction_id is a second row, not an edit to the first."""

    __tablename__ = "commission_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    commission_rate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provider_commission_rates.id"))

    entry_type: Mapped[CommissionEntryType] = mapped_column(
        Enum(CommissionEntryType, name="commission_entry_type"), default=CommissionEntryType.EARNED
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    # Filled in later, separately, when reconciling against what the provider's
    # own statement says they actually paid for this transaction — not set at
    # creation time, since the provider hasn't reported anything yet then.
    provider_reported_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProviderCommissionRate(Base):
    """Versioned commission PayPulse earns FROM a provider on transactions
    routed through it — distinct from anything a merchant pays. Setting a new
    rate never edits an old row in place; it closes the current row out
    (effective_to = now) and inserts a new one, so the rate in effect on any
    past date is always reconstructable. The currently active rate for a
    provider is the one row where effective_to IS NULL."""

    __tablename__ = "provider_commission_rates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    commission_type: Mapped[CommissionType] = mapped_column(Enum(CommissionType, name="commission_type"))
    percentage: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=0)  # 0.0150 = 1.50%
    flat_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    set_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    provider: Mapped["Provider"] = relationship()


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------

class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    shop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shops.id"), nullable=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    merchant_provider_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchant_provider_accounts.id")
    )
    initiated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))

    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType, name="transaction_type"))
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, name="transaction_status"), default=TransactionStatus.INITIATED
    )

    customer_msisdn: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="LSL")

    idempotency_key: Mapped[str] = mapped_column(String(100))
    provider_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Snapshotted at confirmation time from whatever ProviderCommissionRate was
    # active then — never recalculated later, so a subsequent rate change never
    # rewrites what an already-confirmed transaction is recorded as having earned.
    commission_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    commission_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_commission_rates.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["TransactionEvent"]] = relationship(back_populates="transaction")
    receipt: Mapped["Receipt | None"] = relationship(back_populates="transaction", uselist=False)
    shop: Mapped["Shop | None"] = relationship(lazy="joined", viewonly=True)

    @property
    def shop_name(self) -> str | None:
        return self.shop.name if self.shop else None


class TransactionEvent(Base):
    """Append-only audit trail of every state change. Never update rows here, only insert."""

    __tablename__ = "transaction_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    from_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_status: Mapped[str] = mapped_column(String(50))
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actor: Mapped[str] = mapped_column(String(100))  # "system", "callback", or a user id string
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    transaction: Mapped["Transaction"] = relationship(back_populates="events")


class CallbackRaw(Base):
    """Every inbound provider callback, logged before processing. Forensic record."""

    __tablename__ = "callbacks_raw"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    payload: Mapped[dict] = mapped_column(JSONB)
    signature_valid: Mapped[bool] = mapped_column(Boolean)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"), unique=True)
    receipt_number: Mapped[str] = mapped_column(String(50), unique=True)
    printed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reprint_count: Mapped[int] = mapped_column(default=0)

    transaction: Mapped["Transaction"] = relationship(back_populates="receipt")


class DailySettlement(Base):
    """Immutable end-of-day snapshot per merchant-provider-ACCOUNT. Written
    once by a scheduled job.

    Keyed on merchant_provider_account_id, not (merchant_id, provider_id) —
    once a merchant can have multiple shops each with their own till for the
    same provider, keying on the pair alone would collide: two shops' C-Pay
    accounts would fight over the same settlement row for the same day. The
    account is the actual unit being reconciled; merchant_id, shop_id, and
    provider_id are kept here too, denormalized, purely so this table can be
    queried merchant-wide or shop-wide without a join back to the account."""

    __tablename__ = "daily_settlements"
    __table_args__ = (UniqueConstraint("merchant_provider_account_id", "settlement_date"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    shop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shops.id"), nullable=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    merchant_provider_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchant_provider_accounts.id")
    )
    settlement_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    opening_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    total_collections: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    total_withdrawals: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    total_fees: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    provider_reported_closing_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    computed_closing_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    discrepancy: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(150))       # "merchant.approved", "user.created", ...
    target_type: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
