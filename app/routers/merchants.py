import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import get_adapter
from app.config import get_settings
from app.database import get_db
from app.dependencies import CurrentUser, get_current_user, require_roles
from app.models import (
    DailySettlement,
    KycDocStatus,
    Merchant,
    MerchantKycDocument,
    MerchantProviderAccount,
    MerchantStatus,
    Provider,
)
from app.schemas import (
    BalanceOut,
    DailySettlementOut,
    KycDocumentCreate,
    KycDocumentOut,
    KycDocumentReject,
    MerchantCreate,
    MerchantOut,
    MerchantStatusUpdate,
    ProviderAccountActive,
    ProviderAccountCreate,
    ProviderAccountOut,
)
from app.services import audit_service

settings = get_settings()

router = APIRouter(prefix="/merchants", tags=["merchants"])


@router.get("", response_model=list[MerchantOut])
async def list_merchants(
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    """The onboarding/KYC queue — every merchant regardless of status, newest
    first, so pending_kyc ones (needing action) are easy to spot at a glance."""
    merchants = await db.scalars(select(Merchant).order_by(Merchant.created_at.desc()))
    return list(merchants)


@router.get("/{merchant_id}", response_model=MerchantOut)
async def get_merchant(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role != "platform_admin" and user.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")
    return merchant


@router.post("", response_model=MerchantOut, status_code=201)
async def create_merchant(
    body: MerchantCreate,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Onboarding step 1 — the merchant record starts in pending_kyc and can't
    transact until an admin ticks off its KYC documents (see approve_kyc_document
    below) and calls activate_merchant."""
    merchant = Merchant(
        legal_name=body.legal_name,
        trading_name=body.trading_name,
        registration_number=body.registration_number,
        status=MerchantStatus.PENDING_KYC,
    )
    db.add(merchant)
    await db.flush()

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="merchant.created",
        target_type="merchant",
        target_id=str(merchant.id),
        details={"trading_name": merchant.trading_name, "registration_number": merchant.registration_number},
    )

    await db.commit()
    await db.refresh(merchant)
    return merchant


@router.get("/{merchant_id}/kyc-documents", response_model=list[KycDocumentOut])
async def list_kyc_documents(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")
    docs = await db.scalars(
        select(MerchantKycDocument)
        .where(MerchantKycDocument.merchant_id == merchant_id)
        .order_by(MerchantKycDocument.created_at.asc())
    )
    return list(docs)


@router.post("/{merchant_id}/kyc-documents", response_model=KycDocumentOut, status_code=201)
async def add_kyc_document(
    merchant_id: uuid.UUID,
    body: KycDocumentCreate,
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    """Records that a KYC document exists for this merchant with a plain text
    file_reference (a description, not an uploaded file) — kept around mainly
    for scripting/seeding convenience. For an actual file, use the
    /kyc-documents/upload endpoint below instead."""
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    doc = MerchantKycDocument(
        merchant_id=merchant_id,
        doc_type=body.doc_type,
        file_reference=body.file_reference,
        status=KycDocStatus.SUBMITTED,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post("/{merchant_id}/kyc-documents/upload", response_model=KycDocumentOut, status_code=201)
async def upload_kyc_document(
    merchant_id: uuid.UUID,
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    """Real file upload, saved to local disk under kyc_upload_dir. Fine for
    local dev and a small single-server deployment (see the comment on
    Settings.kyc_upload_dir in config.py for what changes at real scale).

    file_reference stores the path *relative* to kyc_upload_dir, not an
    absolute path — so moving the whole upload directory (a new machine, a
    new deploy) doesn't strand every existing record pointing at a path that
    no longer exists."""
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file has no filename")

    merchant_dir = Path(settings.kyc_upload_dir) / str(merchant_id)
    merchant_dir.mkdir(parents=True, exist_ok=True)

    # Prefix with a uuid so two uploads with the same original filename
    # (very likely — "registration-cert.pdf" is a common thing to call a file)
    # never collide on disk.
    safe_name = f"{uuid.uuid4().hex}_{Path(file.filename).name}"
    disk_path = merchant_dir / safe_name

    contents = await file.read()
    disk_path.write_bytes(contents)

    relative_reference = f"{merchant_id}/{safe_name}"
    doc = MerchantKycDocument(
        merchant_id=merchant_id,
        doc_type=doc_type,
        file_reference=relative_reference,
        status=KycDocStatus.SUBMITTED,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.get("/{merchant_id}/kyc-documents/{document_id}/file")
async def download_kyc_document_file(
    merchant_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(MerchantKycDocument, document_id)
    if doc is None or doc.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    disk_path = Path(settings.kyc_upload_dir) / doc.file_reference
    if not disk_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No file on disk for this document — it may have been added via the text-reference "
            "endpoint rather than a real upload, or the upload directory has moved",
        )

    return FileResponse(disk_path, filename=Path(doc.file_reference).name.split("_", 1)[-1])


@router.post("/{merchant_id}/kyc-documents/{document_id}/approve", response_model=KycDocumentOut)
async def approve_kyc_document(
    merchant_id: uuid.UUID,
    document_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(MerchantKycDocument, document_id)
    if doc is None or doc.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # Segregation of duties: whoever captured/submitted the document shouldn't be
    # the same person approving it in a real deployment — enforce that check here
    # once submission is tracked per-user.
    doc.status = KycDocStatus.VERIFIED
    doc.verified_by = user.id
    doc.verified_at = datetime.now(timezone.utc)

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="kyc_document.approved",
        target_type="merchant_kyc_document",
        target_id=str(doc.id),
        details={"merchant_id": str(merchant_id), "doc_type": doc.doc_type},
    )

    await db.commit()
    return doc


@router.post("/{merchant_id}/kyc-documents/{document_id}/reject", response_model=KycDocumentOut)
async def reject_kyc_document(
    merchant_id: uuid.UUID,
    document_id: uuid.UUID,
    body: KycDocumentReject,
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(MerchantKycDocument, document_id)
    if doc is None or doc.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    doc.status = KycDocStatus.REJECTED
    doc.verified_by = user.id
    doc.verified_at = datetime.now(timezone.utc)
    doc.rejection_reason = body.reason

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="kyc_document.rejected",
        target_type="merchant_kyc_document",
        target_id=str(doc.id),
        details={"merchant_id": str(merchant_id), "doc_type": doc.doc_type, "reason": body.reason},
    )

    await db.commit()
    return doc


@router.post("/{merchant_id}/activate", response_model=MerchantOut)
async def activate_merchant(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    docs = await db.scalars(
        select(MerchantKycDocument).where(MerchantKycDocument.merchant_id == merchant_id)
    )
    docs = list(docs)
    if not docs or any(d.status != KycDocStatus.VERIFIED for d in docs):
        raise HTTPException(status_code=400, detail="All KYC documents must be verified first")

    merchant.status = MerchantStatus.ACTIVE

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="merchant.activated",
        target_type="merchant",
        target_id=str(merchant.id),
    )

    await db.commit()
    return merchant


@router.patch("/{merchant_id}/status", response_model=MerchantOut)
async def set_merchant_status(
    merchant_id: uuid.UUID,
    body: MerchantStatusUpdate,
    user: CurrentUser = Depends(require_roles("platform_admin", "compliance_officer")),
    db: AsyncSession = Depends(get_db),
):
    """Suspend or reactivate an already-onboarded merchant. This is the
    correct way to remove a merchant from active operations — never a real
    delete: their transaction, receipt, and commission history all reference
    merchant_id, and a regulated payments business needs to be able to
    produce that full history on request regardless of the merchant's
    current standing. Suspending here still leaves them fully visible in the
    merchant list, just clearly flagged.

    Only active <-> suspended transitions are allowed here. Getting a brand
    new merchant to active for the first time still goes through
    activate_merchant above, which enforces the KYC-verification gate —
    that gate has no business applying to a merchant re-suspending or
    un-suspending an already-verified one."""
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    if body.status not in (MerchantStatus.ACTIVE, MerchantStatus.SUSPENDED):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'suspended' here")
    if merchant.status == MerchantStatus.PENDING_KYC:
        raise HTTPException(
            status_code=400,
            detail="This merchant hasn't completed KYC yet — use the activate endpoint, not this one",
        )

    previous_status = merchant.status.value
    merchant.status = body.status

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="merchant.status_changed",
        target_type="merchant",
        target_id=str(merchant.id),
        details={"from": previous_status, "to": body.status.value},
    )

    await db.commit()
    return merchant


@router.get("/{merchant_id}/provider-accounts", response_model=list[ProviderAccountOut])
async def list_provider_accounts(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lets the frontend (or anyone testing by hand) discover the actual
    merchant_provider_account IDs needed for POST /transactions, rather than
    having to query the database directly."""
    if user.role != "platform_admin" and user.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    query = select(MerchantProviderAccount).where(MerchantProviderAccount.merchant_id == merchant_id)
    if user.role == "teller":
        # Accounts are set up once per merchant and work in every shop; a
        # teller sees those plus anything pinned to their own shop, and never
        # a switched-off account they couldn't use anyway.
        query = query.where(MerchantProviderAccount.is_active.is_(True))
        if user.shop_id is not None:
            query = query.where(
                or_(MerchantProviderAccount.shop_id.is_(None), MerchantProviderAccount.shop_id == user.shop_id)
            )

    accounts = await db.scalars(query.order_by(MerchantProviderAccount.id))
    results = []
    for account in accounts:
        provider = await db.get(Provider, account.provider_id)
        results.append(
            ProviderAccountOut(
                id=account.id,
                shop_id=account.shop_id,
                provider_adapter_key=provider.adapter_key,
                provider_name=provider.name,
                account_identifier=account.account_identifier,
                is_active=account.is_active,
                cached_balance=account.cached_balance,
                balance_updated_at=account.balance_updated_at,
            )
        )
    return results


def _account_out(account: MerchantProviderAccount, provider: Provider) -> ProviderAccountOut:
    return ProviderAccountOut(
        id=account.id,
        shop_id=account.shop_id,
        provider_adapter_key=provider.adapter_key,
        provider_name=provider.name,
        account_identifier=account.account_identifier,
        is_active=account.is_active,
        cached_balance=account.cached_balance,
        balance_updated_at=account.balance_updated_at,
    )


@router.post("/{merchant_id}/provider-accounts", response_model=ProviderAccountOut, status_code=201)
async def create_provider_account(
    merchant_id: uuid.UUID,
    body: ProviderAccountCreate,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Switch a provider on for a merchant. Done once, by PayPulse: from then
    on every shop and every registered device of the merchant can use it —
    merchants don't add or remove providers themselves."""
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    provider = await db.scalar(select(Provider).where(Provider.adapter_key == body.provider_adapter_key))
    if provider is None:
        raise HTTPException(status_code=400, detail=f"Unknown provider adapter_key '{body.provider_adapter_key}'")

    identifier = body.account_identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="account_identifier is required")

    existing = await db.scalar(
        select(MerchantProviderAccount).where(
            MerchantProviderAccount.merchant_id == merchant_id,
            MerchantProviderAccount.provider_id == provider.id,
            MerchantProviderAccount.account_identifier == identifier,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="This account is already registered for this provider")

    account = MerchantProviderAccount(
        merchant_id=merchant_id, shop_id=None, provider_id=provider.id, account_identifier=identifier
    )
    db.add(account)
    await db.flush()
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="provider_account.created",
        target_type="merchant_provider_account",
        target_id=str(account.id),
        details={"merchant_id": str(merchant_id), "provider": provider.name, "account_identifier": identifier},
    )
    await db.commit()
    await db.refresh(account)
    return _account_out(account, provider)


@router.patch("/{merchant_id}/provider-accounts/{account_id}", response_model=ProviderAccountOut)
async def set_provider_account_active(
    merchant_id: uuid.UUID,
    account_id: uuid.UUID,
    body: ProviderAccountActive,
    user: CurrentUser = Depends(require_roles("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Switch a provider off (or back on) for a merchant. It is never deleted:
    past transactions point at it. Off means no device can start a payment
    with it and tellers no longer see it."""
    account = await db.get(MerchantProviderAccount, account_id)
    if account is None or account.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Provider account not found")
    provider = await db.get(Provider, account.provider_id)

    previous = account.is_active
    account.is_active = body.is_active
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="provider_account.enabled" if body.is_active else "provider_account.disabled",
        target_type="merchant_provider_account",
        target_id=str(account.id),
        details={"provider": provider.name, "from": previous, "to": body.is_active},
    )
    await db.commit()
    return _account_out(account, provider)


@router.get("/{merchant_id}/balances", response_model=list[BalanceOut])
async def get_balances(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role != "platform_admin" and user.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    query = select(MerchantProviderAccount).where(
        MerchantProviderAccount.merchant_id == merchant_id, MerchantProviderAccount.is_active.is_(True)
    )
    if user.role == "teller" and user.shop_id is not None:
        query = query.where(
            or_(MerchantProviderAccount.shop_id.is_(None), MerchantProviderAccount.shop_id == user.shop_id)
        )

    accounts = await db.scalars(query)
    results = []
    for account in accounts:
        provider = await db.get(Provider, account.provider_id)
        adapter = get_adapter(provider.adapter_key)
        # One provider that can't report a balance (C-Pay has no balance
        # lookup) or is briefly down must not take the whole page with it.
        try:
            balance = await adapter.get_balance(account.account_identifier)
        except Exception:
            results.append(
                BalanceOut(
                    provider_adapter_key=provider.adapter_key,
                    account_identifier=account.account_identifier,
                    balance=None,
                    as_of=None,
                )
            )
            continue
        # Refresh the cache while we're here so the dashboard has a fast path next time.
        account.cached_balance = balance.amount
        account.balance_updated_at = balance.as_of
        results.append(
            BalanceOut(
                provider_adapter_key=provider.adapter_key,
                account_identifier=account.account_identifier,
                balance=balance.amount,
                as_of=balance.as_of,
            )
        )
    await db.commit()
    return results


@router.get("/{merchant_id}/settlements", response_model=list[DailySettlementOut])
async def list_settlements(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Daily settlement snapshots written by the reconciliation Celery task
    (app/tasks/reconciliation.py) — one row per merchant per provider per day,
    once that job has actually run. Empty until then; this endpoint doesn't
    compute anything live, it only reads what the scheduled job already
    wrote, same as the job intends (see the comment on DailySettlement in
    models.py about why this is a snapshot rather than a live query)."""
    if user.role != "platform_admin" and user.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    rows = await db.scalars(
        select(DailySettlement)
        .where(DailySettlement.merchant_id == merchant_id)
        .order_by(DailySettlement.settlement_date.desc())
    )
    results = []
    for row in rows:
        provider = await db.get(Provider, row.provider_id)
        results.append(
            DailySettlementOut(
                id=row.id,
                merchant_id=row.merchant_id,
                shop_id=row.shop_id,
                provider_id=row.provider_id,
                provider_name=provider.name if provider else "Unknown",
                settlement_date=row.settlement_date,
                opening_balance=row.opening_balance,
                total_collections=row.total_collections,
                total_withdrawals=row.total_withdrawals,
                total_fees=row.total_fees,
                provider_reported_closing_balance=row.provider_reported_closing_balance,
                computed_closing_balance=row.computed_closing_balance,
                discrepancy=row.discrepancy,
                created_at=row.created_at,
            )
        )
    return results
