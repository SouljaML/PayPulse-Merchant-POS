import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

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
    balance: Decimal
    as_of: datetime


class ProviderAccountOut(BaseModel):
    id: uuid.UUID
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


class CommissionRateOut(BaseModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    commission_type: CommissionType
    percentage: Decimal
    flat_fee: Decimal
    effective_from: datetime
    effective_to: datetime | None
    set_by: uuid.UUID

    model_config = {"from_attributes": True}


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
