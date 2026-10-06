"""Seeds enough data to actually use the API locally: platform roles, the five
providers (as mock-backed entries so you can transact without real provider
credentials), a platform admin login, and one demo merchant with two shops,
a teller, a till, and a linked mock provider account.

Safe to run more than once — every insert is guarded by a lookup first, so
re-running just confirms what's already there instead of duplicating it.

Usage:
    python scripts/seed_dev_data.py
"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.security import hash_password
from app.database import AsyncSessionLocal
from app.models import (
    CommissionType,
    KycDocStatus,
    Merchant,
    MerchantKycDocument,
    MerchantProviderAccount,
    MerchantStatus,
    Provider,
    ProviderStatus,
    Role,
    Shop,
    ShopStatus,
    Till,
    TillStatus,
    User,
)
from app.services import commission_service

# Change these before showing this to anyone but yourself — this script exists
# to unblock local development, not to define your real admin credentials.
ADMIN_EMAIL = "admin@paypulse.local"
ADMIN_PASSWORD = "changeme123"
MERCHANT_OWNER_EMAIL = "owner@demo-merchant.local"
MERCHANT_OWNER_PASSWORD = "changeme123"
TELLER_EMAIL = "teller@demo-merchant.local"
TELLER_PASSWORD = "changeme123"

ROLE_NAMES = ["platform_admin", "compliance_officer", "merchant_owner", "teller"]

# adapter_key must match a key registered in app/adapters/registry.py. Each
# provider gets its own key (all currently mock-backed — see registry.py) so
# adapter_key stays unique per provider, matching the DB constraint, and so
# swapping in a real adapter later only ever touches one registry line.
PROVIDERS = [
    ("C-Pay", "cpay"),
    ("M-Pesa", "mpesa"),
    ("EcoCash", "ecocash"),
    ("MyWallet", "mywallet"),
    ("Khetsi", "khetsi"),
]


async def get_or_create_role(db, name: str) -> Role:
    role = await db.scalar(select(Role).where(Role.name == name))
    if role is None:
        role = Role(name=name)
        db.add(role)
        await db.flush()
        print(f"  created role: {name}")
    return role


async def get_or_create_provider(db, name: str, adapter_key: str) -> Provider:
    provider = await db.scalar(select(Provider).where(Provider.name == name))
    if provider is None:
        provider = Provider(name=name, adapter_key=adapter_key, status=ProviderStatus.ACTIVE)
        db.add(provider)
        await db.flush()
        print(f"  created provider: {name} (adapter_key={adapter_key})")
    return provider


async def get_or_create_user(
    db, *, email: str, password: str, full_name: str, role: Role, merchant_id=None, shop_id=None
) -> User:
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            hashed_password=hash_password(password),
            full_name=full_name,
            role_id=role.id,
            merchant_id=merchant_id,
            shop_id=shop_id,
        )
        db.add(user)
        await db.flush()
        print(f"  created user: {email} / {password}  (role={role.name})")
    return user


async def main() -> None:
    async with AsyncSessionLocal() as db:
        print("Roles:")
        roles = {name: await get_or_create_role(db, name) for name in ROLE_NAMES}

        print("Providers:")
        providers = {}
        for name, adapter_key in PROVIDERS:
            providers[name] = await get_or_create_provider(db, name, adapter_key)

        print("Platform admin:")
        admin_user = await get_or_create_user(
            db,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            full_name="Platform Admin",
            role=roles["platform_admin"],
        )

        print("Starter commission rate on C-Pay (2% + LSL 1.50 flat):")
        existing_rate = await commission_service.get_current_rate(db, providers["C-Pay"].id)
        if existing_rate is None:
            await commission_service.set_commission_rate(
                db,
                provider_id=providers["C-Pay"].id,
                commission_type=CommissionType.PERCENTAGE_PLUS_FLAT,
                percentage=Decimal("0.02"),
                flat_fee=Decimal("1.50"),
                set_by=admin_user.id,
            )
            print("  set: 2% + LSL 1.50 flat")

        print("Demo merchant:")
        merchant = await db.scalar(select(Merchant).where(Merchant.registration_number == "DEV-0001"))
        if merchant is None:
            merchant = Merchant(
                legal_name="Demo Merchant Pty",
                trading_name="Demo Shop",
                registration_number="DEV-0001",
                # Skips the real KYC-gate flow (kyc docs -> approve -> activate)
                # on purpose, purely so there's something to log in and
                # transact against locally. Don't seed merchants pre-activated
                # like this anywhere but a dev database.
                status=MerchantStatus.ACTIVE,
            )
            db.add(merchant)
            await db.flush()
            print(f"  created merchant: {merchant.trading_name} ({merchant.id})")

        print("Merchant owner user:")
        await get_or_create_user(
            db,
            email=MERCHANT_OWNER_EMAIL,
            password=MERCHANT_OWNER_PASSWORD,
            full_name="Demo Owner",
            role=roles["merchant_owner"],
            merchant_id=merchant.id,
        )

        print("Shops (Main Branch + Airport Kiosk, to demonstrate multi-shop):")
        main_shop = await db.scalar(
            select(Shop).where(Shop.merchant_id == merchant.id, Shop.name == "Main Branch")
        )
        if main_shop is None:
            main_shop = Shop(merchant_id=merchant.id, name="Main Branch", location="Maseru CBD", status=ShopStatus.ACTIVE)
            db.add(main_shop)
            await db.flush()
            print(f"  created shop: {main_shop.name} ({main_shop.id})")

        airport_shop = await db.scalar(
            select(Shop).where(Shop.merchant_id == merchant.id, Shop.name == "Airport Kiosk")
        )
        if airport_shop is None:
            airport_shop = Shop(
                merchant_id=merchant.id, name="Airport Kiosk", location="Moshoeshoe I Airport", status=ShopStatus.ACTIVE
            )
            db.add(airport_shop)
            await db.flush()
            print(f"  created shop: {airport_shop.name} ({airport_shop.id})")

        print("Teller user (scoped to Main Branch):")
        await get_or_create_user(
            db,
            email=TELLER_EMAIL,
            password=TELLER_PASSWORD,
            full_name="Demo Teller",
            role=roles["teller"],
            merchant_id=merchant.id,
            shop_id=main_shop.id,
        )

        print("Till at Main Branch:")
        till = await db.scalar(
            select(Till).where(Till.merchant_id == merchant.id, Till.till_identifier == "till-001")
        )
        if till is None:
            till = Till(
                merchant_id=merchant.id,
                shop_id=main_shop.id,
                till_identifier="till-001",
                label="Front Counter",
                status=TillStatus.ACTIVE,
            )
            db.add(till)
            await db.flush()
            print(f"  created till: {till.label} ({till.till_identifier})")

        print("Main Branch's mock provider account (C-Pay):")
        account = await db.scalar(
            select(MerchantProviderAccount).where(
                MerchantProviderAccount.merchant_id == merchant.id,
                MerchantProviderAccount.provider_id == providers["C-Pay"].id,
                MerchantProviderAccount.shop_id == main_shop.id,
            )
        )
        if account is None:
            account = MerchantProviderAccount(
                merchant_id=merchant.id,
                shop_id=main_shop.id,
                provider_id=providers["C-Pay"].id,
                account_identifier="26650000000",
            )
            db.add(account)
            await db.flush()
            print(f"  created provider account: {account.account_identifier} on C-Pay (mock)")

        print("A second merchant, still pending KYC (so the admin queue isn't empty):")
        pending_merchant = await db.scalar(
            select(Merchant).where(Merchant.registration_number == "DEV-0002")
        )
        if pending_merchant is None:
            pending_merchant = Merchant(
                legal_name="Newco Traders Pty",
                trading_name="Newco Traders",
                registration_number="DEV-0002",
                status=MerchantStatus.PENDING_KYC,
            )
            db.add(pending_merchant)
            await db.flush()
            print(f"  created merchant: {pending_merchant.trading_name} ({pending_merchant.id})")

        existing_doc = await db.scalar(
            select(MerchantKycDocument).where(MerchantKycDocument.merchant_id == pending_merchant.id)
        )
        if existing_doc is None:
            doc = MerchantKycDocument(
                merchant_id=pending_merchant.id,
                doc_type="business_registration",
                file_reference="newco-registration-cert.pdf",
                status=KycDocStatus.SUBMITTED,
            )
            db.add(doc)
            await db.flush()
            print(f"  created KYC document: {doc.doc_type} (submitted, awaiting approval)")

        await db.commit()

    print("\nDone. Log in at POST /auth/login with:")
    print(f"  platform admin  -> {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    print(f"  merchant owner  -> {MERCHANT_OWNER_EMAIL} / {MERCHANT_OWNER_PASSWORD}")
    print(f"  teller          -> {TELLER_EMAIL} / {TELLER_PASSWORD}  (scoped to Main Branch)")
    print("\nUse the merchant owner or teller token to try POST /transactions against")
    print("the seeded C-Pay (mock) account at Main Branch — no real provider credentials needed.")


if __name__ == "__main__":
    asyncio.run(main())
