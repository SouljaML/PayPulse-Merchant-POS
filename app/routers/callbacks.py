from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Provider
from app.services import transaction_service

router = APIRouter(prefix="/callbacks", tags=["callbacks"])


@router.post("/{provider_adapter_key}")
async def receive_callback(
    provider_adapter_key: str,
    request: Request,
    payload: dict = Body(
        ...,
        openapi_examples={
            "mock_confirmed": {
                "summary": "Mock provider — confirmed",
                "value": {"provider_reference": "MOCK-XXXXXXXXXX", "result": "SUCCESS"},
            },
            "mock_declined": {
                "summary": "Mock provider — declined",
                "value": {
                    "provider_reference": "MOCK-XXXXXXXXXX",
                    "result": "FAILED",
                    "reason": "Customer cancelled",
                },
            },
        },
    ),
    db: AsyncSession = Depends(get_db),
    x_signature: str | None = Header(
        None,
        alias="X-Signature",
        description=(
            "HMAC-SHA256 of the raw request body, hex-encoded, keyed with this "
            "provider's callback_secret. Declared here so it shows up as a "
            "fillable field in /docs — the actual verification still happens "
            "against the full header set in transaction_service.process_callback, "
            "so a real provider sending a differently-named signature header "
            "still works; this parameter exists for local testing convenience, "
            "not because the verification logic requires this exact name."
        ),
    ),
):
    """Providers POST here when a customer confirms or declines a push/OTP prompt.
    This endpoint must respond quickly (providers retry aggressively on timeout) —
    heavy work belongs in a background task, not inline here. Kept synchronous for
    clarity; move the DB write to a Celery task if callback volume grows.

    `payload` is declared as a plain dict (not a Pydantic model) because every
    provider's callback shape is different — normalizing it is the adapter's
    job (see parse_callback in adapters/base.py), not this endpoint's."""

    provider = await db.scalar(select(Provider).where(Provider.adapter_key == provider_adapter_key))
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown provider")

    # FastAPI has already parsed `payload` from the body by this point, which
    # also means the raw bytes are cached on the request — reading them again
    # here returns the exact same bytes that were parsed, which is what the
    # signature must be verified against, not a re-serialization of `payload`
    # (re-serializing could reorder keys or change whitespace and silently
    # invalidate a correctly-computed signature).
    raw_body = await request.body()

    await transaction_service.process_callback(
        db,
        provider_adapter_key=provider_adapter_key,
        provider_id=provider.id,
        raw_payload=payload,
        payload_bytes=raw_body,
        headers=dict(request.headers),
    )

    # Always 200 back to the provider once the callback is logged — even a
    # rejected signature shouldn't trigger the provider's retry storm.
    return {"received": True}