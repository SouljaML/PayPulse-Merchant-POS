"""
C-Pay (Chaperone) adapter — real HTTP implementation, replacing the mock for
the "cpay" provider key.

FLOW — genuinely different from M-Pesa/EcoCash
-------------------------------------------------
Every other provider in this system confirms a transaction either through an
async callback (the customer approves a push notification, the provider POSTs
us a webhook) or by us polling check_status(). C-Pay does neither:

    1. POST {base}/payment  -> C-Pay texts the customer an OTP.
       No money has moved. Response: StatusCode "200", ReasonCode "otpSent".
    2. The teller asks the customer for that OTP and enters it in the POS.
    3. POST {base}/confirm  -> same fields + the real otp.
       This call is SYNCHRONOUS: the HTTP response IS the final result
       (StatusCode "200", ReasonCode "paymentSuccessful" = confirmed money
       movement). C-Pay never calls us back.

That's why `requires_otp = True` exists on this adapter and why
`confirm_collection()` is a new method on BaseProviderAdapter rather than
something handled via the existing callback path — there is no callback to
handle. See transaction_service.confirm_collection_otp() for the service-
layer half of this, and POST /transactions/{id}/confirm-otp in
app/routers/transactions.py for the HTTP endpoint the POS app calls once the
teller has the OTP.

GAP still open: nothing yet tells the POS app that a given provider needs an
OTP screen — adapter.requires_otp only exists on the backend. Whatever
endpoint lists a shop's available providers needs to surface that flag too.
Not fixed in this delivery — need to see that provider-listing
endpoint/schema first.

CHECKSUM — confirmed against real accepted C-Pay UAT requests (not guessed):
    salt     = extTransactionId + clientCode + amount + msisdn + otp   (no separator)
    checksum = hex( HMAC-SHA256(key=secret, message=salt) )
One formula for both /payment (otp="") and /confirm (otp=real code).

KNOWN GAPS — not documented by C-Pay yet, flagged rather than guessed:
    - check_status() now calls a real endpoint (GET {base}/transaction-status)
      but its response shape has never actually been seen — the status
      mapping inside it is inferred from the other two endpoints' envelope,
      not confirmed. Needs a real worked example (ideally one pending, one
      resolved) to verify before it's trusted for reconciliation.
    - No balance endpoint: get_balance() raises NotImplementedError.
    - No callback/webhook of any kind: verify_callback_signature() and
      parse_callback() raise NotImplementedError and should never be called
      for this adapter — process_callback() in transaction_service is simply
      not part of C-Pay's flow.
    - Wrong-OTP decline shape is now CONFIRMED (real UAT attempt): it comes
      back as HTTP 422 (not 200) wrapping {"StatusCode": "422", "ReasonCode":
      "invalidPassword", "Description": "Payment Failed. Operation failed
      due to invalid password! Please verify OTP is correct"}. See
      map_confirm_response() and confirm_collection(). Other rejection
      reasons (e.g. insufficient funds, expired OTP) are still UNSEEN —
      they're assumed to land in the same envelope/DECLINED bucket but with
      a different ReasonCode, not yet confirmed.
    - "subscriptions" must be omitted from the request body entirely — even
      sending the documented default object causes C-Pay to reject the
      request. Confirmed empirically; don't reintroduce it.
"""

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.adapters.base import (
    Balance,
    BaseProviderAdapter,
    CallbackResult,
    NormalizedStatus,
    ProviderResponse,
)

logger = logging.getLogger(__name__)


def format_amount(amount: Decimal) -> str:
    return f"{Decimal(amount):.2f}"


def compute_checksum(
    *, ext_transaction_id: str, client_code: str, amount: str, msisdn: str, otp: str, secret: str
) -> str:
    """amount must already be the exact string going in the request body —
    callers should run it through format_amount() first, same value both
    places, or the signature C-Pay computes won't match ours."""
    salt = f"{ext_transaction_id}{client_code}{amount}{msisdn}{otp}"
    return hmac.new(secret.encode("utf-8"), salt.encode("utf-8"), hashlib.sha256).hexdigest()


def map_confirm_response(data: dict) -> NormalizedStatus:
    """Pure mapping for /confirm's response envelope, pulled out for
    testability the same way map_status_response() is for /transaction-status.

    CONFIRMED: paymentSuccessful, as before.
    DECLINED: covers the confirmed wrong-OTP shape — HTTP 422 wrapping
    {"StatusCode": "422", "ReasonCode": "invalidPassword", "Description":
    "Payment Failed. Operation failed due to invalid password! Please
    verify OTP is correct"} — verified against a real C-Pay UAT attempt.
    Anything else landing in this envelope that isn't the success shape is
    still treated as DECLINED on the same best-effort basis as before;
    only invalidPassword is proven, other rejection reasons (e.g.
    insufficient funds) may use a different ReasonCode not yet seen.
    """
    if data.get("StatusCode") == "200" and data.get("ReasonCode") == "paymentSuccessful":
        return NormalizedStatus.CONFIRMED
    return NormalizedStatus.DECLINED


def map_status_response(data: dict) -> NormalizedStatus:
    """Pure mapping, pulled out of check_status() so it's testable without
    mocking an HTTP call. See check_status()'s docstring for which branches
    are verified vs. inferred.

    CONFIRMED: verified against one real resolved-transaction response.
    Everything else: inferred, not yet seen for real.
    """
    if data.get("PaymentRequestStatus") == "processed" and data.get("ReasonCode") == "paymentComplete":
        return NormalizedStatus.CONFIRMED

    reason_code = data.get("ReasonCode", "")
    if reason_code in ("otpSent", "pending", "paymentPending") or data.get("PaymentRequestStatus") is None:
        return NormalizedStatus.PENDING
    if data.get("StatusCode") != "200":
        return NormalizedStatus.FAILED
    return NormalizedStatus.DECLINED


class CPayAdapter(BaseProviderAdapter):
    adapter_key = "cpay"
    requires_otp = True

    def __init__(self, *, base_url: str, api_key: str, secret: str, client_code: str, currency: str = "LSL"):
        # base_url is expected to already include the full API path, e.g.
        # "https://cpay-uat-env.chaperone.co.ls:5100/api/cpaypayments" — this
        # adapter just appends "/payment" or "/confirm" to it, nothing more.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._secret = secret
        self._client_code = client_code
        self._currency = currency

    def _headers(self) -> dict:
        return {
            "accept": "text/plain",
            "Authorization": self._api_key,
            "Content-Type": "application/json-patch+json",
        }

    def _build_body(self, *, ext_transaction_id: str, msisdn: str, otp: str, amount_str: str) -> dict:
        checksum = compute_checksum(
            ext_transaction_id=ext_transaction_id,
            client_code=self._client_code,
            amount=amount_str,
            msisdn=msisdn,
            otp=otp,
            secret=self._secret,
        )
        return {
            "transactionRequest": {
                "extTransactionId": ext_transaction_id,
                "clientCode": self._client_code,
                "msisdn": msisdn,
                "otp": otp,
                "amount": amount_str,
                "shortDescription": "",
                "checksum": checksum,
                "currency": self._currency,
                "otpMedium": "sms",
                "additionalData": "",
                "redirectUrl": "",
                # subscriptions intentionally omitted — see module docstring.
            }
        }

    async def initiate_collection(
        self, *, account_identifier: str, customer_msisdn: str, amount: Decimal, txn_ref: str
    ) -> ProviderResponse:
        amount_str = format_amount(amount)
        body = self._build_body(ext_transaction_id=txn_ref, msisdn=customer_msisdn, otp="", amount_str=amount_str)

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{self._base_url}/payment",
                params={"cardPayment": "false", "rememberMe": "false"},
                headers=self._headers(),
                json=body,
            )
        resp.raise_for_status()
        data = resp.json().get("return", {})

        if data.get("StatusCode") == "200" and data.get("ReasonCode") == "otpSent":
            # provider_reference = our own txn_ref. CPayTransactionId doesn't
            # exist yet at this point — it's null until /confirm succeeds —
            # so txn_ref is the only stable identifier we have to look this
            # transaction up by later.
            return ProviderResponse(provider_reference=txn_ref, status=NormalizedStatus.PENDING, raw=data)

        raise RuntimeError(
            data.get("Description") or data.get("ReasonCode") or "C-Pay rejected the payment request"
        )

    async def confirm_collection(
        self, *, provider_reference: str, otp: str, customer_msisdn: str, amount: Decimal, txn_ref: str
    ) -> ProviderResponse:
        amount_str = format_amount(amount)
        # extTransactionId must be the SAME value sent on /payment — that's
        # provider_reference here, not a fresh txn_ref.
        body = self._build_body(
            ext_transaction_id=provider_reference, msisdn=customer_msisdn, otp=otp, amount_str=amount_str
        )

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{self._base_url}/confirm",
                headers=self._headers(),
                json=body,
            )

        # IMPORTANT: a rejected /confirm (e.g. wrong OTP) comes back as HTTP
        # 422, NOT a 200 — but it's still the same {"return": {...}} envelope
        # as a success. Confirmed against a real wrong-OTP UAT attempt:
        # 422 wrapping {"StatusCode": "422", "ReasonCode": "invalidPassword",
        # "Description": "Payment Failed. Operation failed due to invalid
        # password! Please verify OTP is correct"}. So the envelope is
        # parsed FIRST regardless of HTTP status — calling raise_for_status()
        # before this (the previous bug) throws before the body is ever
        # read, and that exception then gets swallowed by the generic
        # `except Exception` in confirm_collection_otp(), which turns a
        # clean decline into a transaction stuck as FAILED with a raw httpx
        # error string as its decline_reason instead of the real provider
        # message above.
        try:
            data = resp.json().get("return", {})
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"C-Pay /confirm returned a non-JSON response: {resp.text!r}")

        if not data:
            # No envelope at all — a response shape C-Pay hasn't shown us
            # (e.g. a 5xx from infra in front of it, not from C-Pay itself).
            # Let the real HTTP error surface rather than guessing.
            resp.raise_for_status()

        return ProviderResponse(provider_reference=provider_reference, status=map_confirm_response(data), raw=data)

    async def initiate_withdrawal(self, *, account_identifier: str, amount: Decimal, txn_ref: str) -> ProviderResponse:
        raise NotImplementedError("C-Pay withdrawal/payout endpoint not documented yet")

    async def check_status(self, provider_reference: str, *, requested_at: datetime | None = None) -> NormalizedStatus:
        """UNVERIFIED — calls GET {base}/transaction-status?requestReference=
        <provider_reference>&dateTime=<YYYY-MM-DD>, but no real example of
        this endpoint's response has been seen. The mapping below assumes it
        reuses the same {"return": {"StatusCode", "ReasonCode", ...}}
        envelope as /payment and /confirm, since that's the only pattern
        we've actually observed from this API — reasonable, but a guess.
        Get one real response (a pending one and, ideally, a resolved one)
        and this can be made exact instead of best-effort.

        requested_at should be the date the original /payment call was made,
        IF you have it. A real call made with only requestReference (no
        dateTime at all) got a clean 200 back, so dateTime looks optional
        despite appearing in the original docs fragment — omitted entirely
        here rather than guessed at (e.g. defaulting to "today"), since a
        wrong guessed date seems more likely to cause a problem than no
        date param at all.

        STATUS MAPPING — confirmed for the CONFIRMED case only, from one
        real response for a resolved transaction:
            {"StatusCode": "200", "PaymentRequestStatus": "processed",
             "ReasonCode": "paymentComplete", "additionalData": {...}}
        Note this endpoint's own vocabulary ("paymentComplete") differs from
        /confirm's ("paymentSuccessful") — they are NOT interchangeable, a
        real mistake an earlier guess made here before this was verified.
        PENDING and FAILED/DECLINED mappings below are still inferred, not
        proven — no real example of a still-pending or a failed/declined
        transaction-status response has been seen yet."""
        params = {"requestReference": provider_reference}
        if requested_at is not None:
            params["dateTime"] = requested_at.strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{self._base_url}/transaction-status",
                params=params,
                headers=self._headers(),
            )
        resp.raise_for_status()
        data = resp.json().get("return", {})
        return map_status_response(data)

    async def get_balance(self, account_identifier: str) -> Balance:
        raise NotImplementedError("C-Pay balance endpoint not documented yet")

    def verify_callback_signature(self, payload: bytes, headers: dict) -> bool:
        raise NotImplementedError("C-Pay has no callback — confirm_collection() resolves synchronously instead")

    def parse_callback(self, payload: dict) -> CallbackResult:
        raise NotImplementedError("C-Pay has no callback — confirm_collection() resolves synchronously instead")
