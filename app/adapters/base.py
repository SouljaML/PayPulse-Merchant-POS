from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class NormalizedStatus(str, Enum):
    """Every provider's status vocabulary gets mapped into this before the rest of
    the system sees it. Nothing outside the adapter layer should ever branch on a
    provider-specific string."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    FAILED = "failed"


@dataclass
class ProviderResponse:
    """Result of initiating a transaction with a provider."""

    provider_reference: str
    status: NormalizedStatus
    raw: dict


@dataclass
class CallbackResult:
    """Result of parsing an inbound provider callback."""

    provider_reference: str
    status: NormalizedStatus
    reason: str | None = None


@dataclass
class Balance:
    account_identifier: str
    amount: Decimal
    as_of: datetime


class BaseProviderAdapter(ABC):
    """One implementation per mobile money provider. The transaction service only
    ever talks to this interface, never to a provider's SDK/API directly."""

    adapter_key: str

    # True for providers whose collection flow needs a human-entered OTP as a
    # second step (C-Pay) rather than resolving via an async push/callback
    # (M-Pesa, EcoCash). transaction_service and the POS UI use this flag to
    # decide whether to prompt for an OTP after initiate_collection() returns
    # PENDING. Defaults False so every existing adapter is unaffected.
    requires_otp: bool = False

    @abstractmethod
    async def initiate_collection(
        self, *, account_identifier: str, customer_msisdn: str, amount: Decimal, txn_ref: str
    ) -> ProviderResponse:
        """Request money FROM a customer INTO the merchant's provider account.
        Triggers a push notification or OTP prompt on the customer's phone."""
        ...

    async def confirm_collection(
        self, *, provider_reference: str, otp: str, customer_msisdn: str, amount: Decimal, txn_ref: str
    ) -> ProviderResponse:
        """Second step for an adapter with requires_otp=True: the customer
        received an OTP out-of-band after initiate_collection(), the teller
        entered it, and this call resolves the transaction — synchronously,
        for providers like C-Pay that have no callback at all.

        Concrete (not abstract) so adapters that don't need this — anything
        with requires_otp=False — don't have to implement a no-op override.
        Calling it on such an adapter is a bug in the caller, so this raises
        rather than silently doing nothing."""
        raise NotImplementedError(f"{type(self).__name__} does not use a separate OTP confirmation step")

    @abstractmethod
    async def initiate_withdrawal(
        self, *, account_identifier: str, amount: Decimal, txn_ref: str
    ) -> ProviderResponse:
        """Move money OUT of the merchant's provider account."""
        ...

    @abstractmethod
    async def check_status(self, provider_reference: str, *, requested_at: datetime | None = None) -> NormalizedStatus:
        """Poll the provider directly — used by the reconciliation job to catch
        transactions where a callback was lost.

        requested_at: optional — the date the original transaction was
        initiated. Added for C-Pay's status endpoint, which takes a date
        param alongside the reference; adapters that don't need it just
        ignore it. Defaults to None so every existing call site/override
        is unaffected."""
        ...

    @abstractmethod
    async def get_balance(self, account_identifier: str) -> Balance:
        ...

    @abstractmethod
    def verify_callback_signature(self, payload: bytes, headers: dict) -> bool:
        """Every provider signs callbacks differently (HMAC header, shared secret in
        body, mTLS...). Reject anything that doesn't verify before trusting it."""
        ...

    @abstractmethod
    def parse_callback(self, payload: dict) -> CallbackResult:
        ...
