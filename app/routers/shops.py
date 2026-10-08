import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentUser, get_current_user, require_merchant_manager
from app.models import Merchant, MerchantProviderAccount, Provider, Shop, ShopStatus
from app.schemas import ProviderAccountOut, ShopCreate, ShopOut, ShopStatusUpdate
from app.services import audit_service

router = APIRouter(prefix="/merchants/{merchant_id}/shops", tags=["shops"])


@router.get("", response_model=list[ShopOut])
async def list_shops(
    merchant_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role != "platform_admin" and user.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")
    shops = await db.scalars(select(Shop).where(Shop.merchant_id == merchant_id).order_by(Shop.created_at.asc()))
    return list(shops)


@router.post("", response_model=ShopOut, status_code=201)
async def create_shop(
    merchant_id: uuid.UUID,
    body: ShopCreate,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")

    existing = await db.scalar(select(Shop).where(Shop.merchant_id == merchant_id, Shop.name == body.name))
    if existing is not None:
        raise HTTPException(status_code=409, detail="A shop with this name already exists for this merchant")

    shop = Shop(merchant_id=merchant_id, name=body.name, location=body.location, status=ShopStatus.ACTIVE)
    db.add(shop)
    await db.flush()

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="shop.created",
        target_type="shop",
        target_id=str(shop.id),
        details={"merchant_id": str(merchant_id), "name": shop.name},
    )

    await db.commit()
    await db.refresh(shop)
    return shop


@router.patch("/{shop_id}/status", response_model=ShopOut)
async def set_shop_status(
    merchant_id: uuid.UUID,
    shop_id: uuid.UUID,
    body: ShopStatusUpdate,
    user: CurrentUser = Depends(require_merchant_manager),
    db: AsyncSession = Depends(get_db),
):
    shop = await db.get(Shop, shop_id)
    if shop is None or shop.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Shop not found")

    try:
        new_status = ShopStatus(body.status)
    except ValueError:
        raise HTTPException(status_code=400, detail="status must be 'active' or 'suspended'")

    previous = shop.status.value
    shop.status = new_status

    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="shop.status_changed",
        target_type="shop",
        target_id=str(shop.id),
        details={"from": previous, "to": new_status.value},
    )

    await db.commit()
    return shop


@router.get("/{shop_id}/provider-accounts", response_model=list[ProviderAccountOut])
async def list_shop_provider_accounts(
    merchant_id: uuid.UUID,
    shop_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role != "platform_admin" and user.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    shop = await db.get(Shop, shop_id)
    if shop is None or shop.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Shop not found")

    # Providers are set up once for the whole merchant (shop_id empty) and work
    # in every shop; an account pinned to this shop is also included.
    accounts = await db.scalars(
        select(MerchantProviderAccount).where(
            MerchantProviderAccount.merchant_id == merchant_id,
            or_(MerchantProviderAccount.shop_id.is_(None), MerchantProviderAccount.shop_id == shop_id),
        )
    )
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
