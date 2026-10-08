import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import CommissionType, KycDocStatus, MerchantStatus, TransactionStatus, TransactionType, ProviderStatus


class TransactionCreate(BaseModel):
    provider_adapter_key: str = Field(..., examples=["mpesa", "ecocash", "cpay", "mywallet", "khetsi"])
    merchant_provider_account_id: uuid.UUID
    customer_msisdn: str
    amount: Decimal
    type: TransactionType = TransactionType.COLLECTION
    idempotency_key: str
    device_id: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be greater than zero")
        return v

    @field_validator("customer_msisdn")
    @classmethod
    def msisdn_basic_shape(cls, v: str) -> str:
        digits = v.replace("+", "").strip()
        if not digits.isdigit() or not (8 <= len(digits) <= 15):
            raise ValueError("customer_msisdn does not look like a valid phone number")
        return v


class TransactionOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    shop_id: uuid.UUID | None
    shop_name: str | None = None
    provider_id: uuid.UUID
    type: TransactionType
    status: TransactionStatus
    provider_reference: str | None
    amount: Decimal
    currency: str
    customer_msisdn: str
    created_at: datetime
    confirmed_at: datetime | None
    decline_reason: str | None

    model_config = {"from_attributes": True}


class ReceiptOut(BaseModel):
    receipt_number: str
    transaction: TransactionOut

    model_config = {"from_attributes": True}


class WithdrawalCreate(BaseModel):
    provider_adapter_key: str
    merchant_provider_account_id: uuid.UUID
    amount: Decimal
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be greater than zero")
        return v


class BalanceOut(BaseModel):
    provider_adapter_key: str
    account_identifier: str
    # None when the provider has no balance lookup (C-Pay) or it failed.
    balance: Decimal | None = None
    as_of: datetime | None = None


class ProviderAccountOut(BaseModel):
    id: uuid.UUID
    # None = available to every shop of the merchant (the normal case).
    shop_id: uuid.UUID | None = None
    provider_adapter_key: str
    provider_name: str
    account_identifier: str
    is_active: bool
    cached_balance: Decimal | None
    balance_updated_at: datetime | None

    model_config = {"from_attributes": True}


class ProviderCreate(BaseModel):
    name: str
    adapter_key: str

class ProviderOut(BaseModel):
    id: uuid.UUID
    name: str
    adapter_key: str
    status: ProviderStatus
    # Not a DB column — computed in the router from the live adapter
    # registry's requires_otp attribute (see BaseProviderAdapter). Tells the
    # POS app whether this provider needs an OTP-entry step after the
    # initial submit (currently true only for C-Pay) or resolves via push +
    # callback like M-Pesa/EcoCash.
    requires_otp: bool



class MerchantOut(BaseModel):
    id: uuid.UUID
    legal_name: str
    trading_name: str
    registration_number: str
    status: MerchantStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class MerchantCreate(BaseModel):
    legal_name: str
    trading_name: str
    registration_number: str


class MerchantStatusUpdate(BaseModel):
    status: MerchantStatus


class KycDocumentOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    doc_type: str
    file_reference: str
    status: KycDocStatus
    verified_by: uuid.UUID | None
    verified_at: datetime | None
    rejection_reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class KycDocumentCreate(BaseModel):
    doc_type: str
    # Stands in for a real upload until object storage exists (see README) —
    # just a text reference for now, e.g. a filename or description.
    file_reference: str


class KycDocumentReject(BaseModel):
    reason: str


class CommissionRateCreate(BaseModel):
    commission_type: CommissionType
    percentage: Decimal = Decimal("0")
    flat_fee: Decimal = Decimal("0")

    @field_validator("percentage")
    @classmethod
    def percentage_in_range(cls, v: Decimal) -> Decimal:
        if not (0 <= v <= 1):
            raise ValueError("percentage must be between 0 and 1 (e.g. 0.015 for 1.5%), not a 0-100 value")
        return v

    @field_validator("flat_fee")
    @classmethod
    def flat_fee_not_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("flat_fee cannot be negative")
        return v


class CommissionTierIn(BaseModel):
    """One band. commission = provider_fee * percentage + flat_fee, where
    provider_fee is what the provider charges for amounts in this band and
    percentage is PayPulse's share of that fee (0.20 = 20%)."""

    min_amount: Decimal
    max_amount: Decimal | None = None  # None = no upper limit
    provider_fee: Decimal = Decimal("0")
    percentage: Decimal = Decimal("0")
    flat_fee: Decimal = Decimal("0")

    @field_validator("percentage")
    @classmethod
    def tier_percentage_in_range(cls, v: Decimal) -> Decimal:
        if not (0 <= v <= 1):
            raise ValueError("percentage must be between 0 and 1 (0.20 = 20% of the provider's fee), not a 0-100 value")
        return v

    @field_validator("min_amount", "max_amount", "provider_fee", "flat_fee")
    @classmethod
    def not_negative(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v < 0:
            raise ValueError("amounts cannot be negative")
        return v


class CommissionTiersSet(BaseModel):
    """The full replacement set of bands for a provider. A band covers
    min_amount through max_amount, BOTH inclusive (amounts have two decimals,
    so a schedule written "1-100, 100.01-500" has no hole between its bands).
    Rules: bands can't overlap (a band's min must be above the previous band's
    max); only the highest band may have no upper limit; max can't be below
    min. Gaps are allowed — an amount in a gap simply earns no commission,
    the same as an amount above a capped top band."""

    tiers: list[CommissionTierIn]

    @model_validator(mode="after")
    def bands_are_valid(self) -> "CommissionTiersSet":
        tiers = sorted(self.tiers, key=lambda t: t.min_amount)
        if not tiers:
            raise ValueError("at least one band is required")
        for i, t in enumerate(tiers):
            last = i == len(tiers) - 1
            if t.max_amount is not None and t.max_amount < t.min_amount:
                raise ValueError(f"band starting at {t.min_amount}: max amount can't be below min amount")
            if last:
                continue
            nxt = tiers[i + 1]
            if t.max_amount is None:
                raise ValueError("only the highest band can have no max amount")
            if nxt.min_amount <= t.max_amount:
                raise ValueError(
                    f"bands overlap: the band {t.min_amount} to {t.max_amount} runs into the band starting at "
                    f"{nxt.min_amount}"
                )
        self.tiers = tiers
        return self


class CommissionTierOut(BaseModel):
    id: uuid.UUID
    min_amount: Decimal
    max_amount: Decimal | None
    provider_fee: Decimal
    percentage: Decimal
    flat_fee: Decimal

    model_config = {"from_attributes": True}


class CommissionRateOut(BaseModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    commission_type: CommissionType
    percentage: Decimal
    flat_fee: Decimal
    effective_from: datetime
    effective_to: datetime | None
    set_by: uuid.UUID
    # Empty = plain single rate; otherwise the bands decide and the three
    # fields above are ignored.
    tiers: list[CommissionTierOut] = []

    model_config = {"from_attributes": True}


class CommissionPreviewOut(BaseModel):
    provider_id: uuid.UUID
    amount: Decimal
    commission: Decimal | None  # None = no rate / amount outside every band: nothing would be recorded
    provider_fee: Decimal | None = None  # the band's provider fee; None for a plain (untiered) rate
    rate_id: uuid.UUID | None


class CommissionSummaryOut(BaseModel):
    provider_id: uuid.UUID
    provider_name: str
    total_commission: Decimal
    transaction_count: int


class CommissionEntryOut(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    provider_id: uuid.UUID
    commission_rate_id: uuid.UUID
    entry_type: str
    amount: Decimal
    provider_reported_amount: Decimal | None
    reconciled_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def new_password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("new_password must be at least 8 characters")
        return v


class StaffUserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class StaffUserCreate(BaseModel):
    email: str
    password: str
    full_name: str
    role: str  # validated against STAFF_ROLES in the router — platform staff only

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class StaffUserStatusUpdate(BaseModel):
    is_active: bool


class AuditLogOut(BaseModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    action: str
    target_type: str
    target_id: str
    details: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class DailySettlementOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    shop_id: uuid.UUID | None
    provider_id: uuid.UUID
    provider_name: str
    settlement_date: datetime
    opening_balance: Decimal
    total_collections: Decimal
    total_withdrawals: Decimal
    total_fees: Decimal
    provider_reported_closing_balance: Decimal | None
    computed_closing_balance: Decimal
    discrepancy: Decimal
    created_at: datetime


# ---- Shops ----

class ShopOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    name: str
    location: str | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ShopCreate(BaseModel):
    name: str
    location: str | None = None


class ShopStatusUpdate(BaseModel):
    status: str  # "active" | "suspended" — validated against ShopStatus in the router


# ---- Provider accounts (tills, in the money sense — per shop, per provider) ----

class ProviderAccountCreate(BaseModel):
    provider_adapter_key: str
    account_identifier: str


class ProviderAccountActive(BaseModel):
    is_active: bool


# ---- Tills (physical checkout terminals — the fraud-control unit) ----

class TillOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    shop_id: uuid.UUID
    till_identifier: str
    label: str
    status: str
    blocked_reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TillCreate(BaseModel):
    shop_id: uuid.UUID
    till_identifier: str
    label: str


class TillStatusUpdate(BaseModel):
    status: str  # "active" | "blocked"
    reason: str | None = None  # required by the router when status == "blocked"


# ---- Tellers (merchant-scoped users — distinct from platform staff in users.py) ----

class TellerOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    shop_id: uuid.UUID | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TellerCreate(BaseModel):
    email: str
    password: str
    full_name: str
    role: str = "teller"  # "teller" or "merchant_owner" (a co-owner)
    shop_id: uuid.UUID | None = None

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class TellerStatusUpdate(BaseModel):
    is_active: bool


class TellerShopAssign(BaseModel):
    shop_id: uuid.UUID | None


class ResetPasswordRequest(BaseModel):
    email: str


class ResetPasswordOut(BaseModel):
    email: str
    full_name: str
    role: str
    temporary_password: str


# ---- Devices (registered phones / POS terminals) ----

class DeviceCreate(BaseModel):
    shop_id: uuid.UUID
    till_id: uuid.UUID | None = None
    label: str = Field(..., min_length=1, max_length=150)


class DeviceRevoke(BaseModel):
    reason: str = Field(..., min_length=1, max_length=300)


class DeviceOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    shop_id: uuid.UUID
    shop_name: str | None = None
    till_id: uuid.UUID | None = None
    till_label: str | None = None
    label: str
    status: str
    platform: str | None = None
    model: str | None = None
    os_version: str | None = None
    app_version: str | None = None
    created_at: datetime
    enrolled_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    enrollment_expires_at: datetime | None = None

    model_config = {"from_attributes": True}


class DeviceWithCodeOut(DeviceOut):
    """Returned once, when a device is registered or its code is reissued.
    The code is not stored in readable form, so it can't be shown again."""

    enrollment_code: str


class DeviceEnrollRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=32)
    hardware_id: str | None = Field(None, max_length=200)
    platform: str | None = Field(None, max_length=50)
    model: str | None = Field(None, max_length=150)
    os_version: str | None = Field(None, max_length=50)
    app_version: str | None = Field(None, max_length=50)


class DeviceEnrollOut(BaseModel):
    device_id: uuid.UUID
    device_token: str
    label: str
    merchant_id: uuid.UUID
    shop_id: uuid.UUID
    shop_name: str | None = None
    till_id: uuid.UUID | None = None
    till_identifier: str | None = None
    till_label: str | None = None
