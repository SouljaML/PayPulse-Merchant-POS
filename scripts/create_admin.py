"""Production bootstrap: creates the four roles and ONE platform admin.
Does not create any demo merchants, providers or passwords (unlike seed_dev_data.py).

The password is read from the PAYPULSE_ADMIN_PASSWORD environment variable, or
prompted for, so it never appears in your shell history or in this file.

Usage:
    PAYPULSE_ADMIN_EMAIL=you@company.com python scripts/create_admin.py
"""

import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.security import hash_password
from app.database import AsyncSessionLocal
from app.models import Provider, ProviderStatus, Role, User

ROLE_NAMES = ["platform_admin", "compliance_officer", "merchant_owner", "teller"]
PROVIDERS = [("C-Pay", "cpay"), ("M-Pesa", "mpesa"), ("EcoCash", "ecocash"), ("MyWallet", "mywallet"), ("Khetsi", "khetsi")]


async def main() -> None:
    email = os.environ.get("PAYPULSE_ADMIN_EMAIL") or input("Admin email: ").strip()
    name = os.environ.get("PAYPULSE_ADMIN_NAME", "Platform Admin")
    password = os.environ.get("PAYPULSE_ADMIN_PASSWORD") or getpass.getpass("Admin password (min 12 chars): ")
    if len(password) < 12:
        sys.exit("Password must be at least 12 characters.")

    async with AsyncSessionLocal() as db:
        roles = {}
        for rn in ROLE_NAMES:
            role = await db.scalar(select(Role).where(Role.name == rn))
            if role is None:
                role = Role(name=rn)
                db.add(role)
                await db.flush()
            roles[rn] = role
        # The provider catalogue (names only; each is switched on per merchant by an admin).
        for pname, key in PROVIDERS:
            if await db.scalar(select(Provider).where(Provider.name == pname)) is None:
                db.add(Provider(name=pname, adapter_key=key, status=ProviderStatus.ACTIVE))
        if await db.scalar(select(User).where(User.email == email)) is not None:
            sys.exit(f"{email} already exists; nothing changed.")
        db.add(User(email=email, hashed_password=hash_password(password), full_name=name, role_id=roles["platform_admin"].id))
        await db.commit()
    print(f"Created platform admin {email}.")


if __name__ == "__main__":
    asyncio.run(main())
