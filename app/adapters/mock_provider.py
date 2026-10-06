import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.config import get_settings
from app.adapters.base import (
    BaseProviderAdapter,
    Balance,
    CallbackResult,
    NormalizedStatus,
    ProviderResponse,
)


logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


class MockProviderAdapter(BaseProviderAdapter):
    adapter_key = "mock"

    def __init__(self, callback_secret: str = "dev-secret"):
        self._callback_secret = callback_secret

    async def initiate_collection(
        self, *, account_identifier: str, customer_msisdn: str, amount: Decimal, txn_ref: str
    ) -> ProviderResponse:
        provider_reference = f"MOCK-{uuid.uuid4().hex[:10].upper()}"
        self._maybe_schedule_reply(provider_reference, customer_msisdn)
        return ProviderResponse(
            provider_reference=provider_reference,
            status=NormalizedStatus.PENDING,
            raw={"echo": txn_ref},
        )

    async def initiate_withdrawal(
        self, *, account_identifier: str, amount: Decimal, txn_ref: str
    ) -> ProviderResponse:
        provider_reference = f"MOCK-WD-{uuid.uuid4().hex[:10].upper()}"
        self._maybe_schedule_reply(provider_reference, "")
        return ProviderResponse(
            provider_reference=provider_reference,
            status=NormalizedStatus.PENDING,
            raw={"echo": txn_ref},
        )

    async def check_status(self, provider_reference: str, *, requested_at: datetime | None = None) -> NormalizedStatus:
        return NormalizedStatus.PENDING

    async def get_balance(self, account_identifier: str) -> Balance:
        return Balance(
            account_identifier=account_identifier,
            amount=Decimal("1000.00"),
            as_of=datetime.now(timezone.utc),
        )

    def verify_callback_signature(self, payload: bytes, headers: dict) -> bool:
        expected = hmac.new(self._callback_secret.encode(), payload, hashlib.sha256).hexdigest()
        provided = headers.get("x-signature", "")
        return hmac.compare_digest(expected, provided)

    def parse_callback(self, payload: dict) -> CallbackResult:
        status_map = {
            "SUCCESS": NormalizedStatus.CONFIRMED,
            "FAILED": NormalizedStatus.DECLINED,
        }
        return CallbackResult(
            provider_reference=payload["provider_reference"],
            status=status_map.get(payload.get("result"), NormalizedStatus.FAILED),
            reason=payload.get("reason"),
        )

    def _maybe_schedule_reply(self, provider_reference: str, customer_msisdn: str) -> None:
        delay = get_settings().mock_auto_confirm_seconds
        if delay <= 0:
            return
        task = asyncio.create_task(self._reply_after(delay, provider_reference, customer_msisdn))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _reply_after(self, delay: float, provider_reference: str, customer_msisdn: str) -> None:
        try:
            await asyncio.sleep(delay)
            from sqlalchemy import select
            from app.database import AsyncSessionLocal
            from app.models import Provider, Transaction
            from app.services import transaction_service

            declines = customer_msisdn.endswith("0000")
            payload = {"provider_reference": provider_reference, "result": "FAILED" if declines else "SUCCESS"}
            if declines:
                payload["reason"] = "Customer declined the request"
            body = json.dumps(payload).encode()
            signature = hmac.new(self._callback_secret.encode(), body, hashlib.sha256).hexdigest()

            async with AsyncSessionLocal() as db:
                txn = None
                for _ in range(6):
                    txn = await db.scalar(select(Transaction).where(Transaction.provider_reference == provider_reference))
                    if txn is not None:
                        break
                    await asyncio.sleep(0.5)
                if txn is None:
                    return
                provider = await db.get(Provider, txn.provider_id)
                await transaction_service.process_callback(
                    db,
                    provider_adapter_key=provider.adapter_key,
                    provider_id=provider.id,
                    raw_payload=payload,
                    payload_bytes=body,
                    headers={"x-signature": signature},
                )
        except Exception:
            logger.exception("mock provider demo reply failed for %s", provider_reference)
