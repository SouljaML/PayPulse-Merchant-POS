"""One-off: turn existing per-shop provider accounts into merchant-wide ones,
so every shop and device of the merchant can use them.

Safe to run: past transactions keep the shop they were made in (that is stored
on the transaction itself). Run with --yes to apply; without it, only reports.

    python scripts/make_provider_accounts_merchant_wide.py --yes
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import update, select, func

from app.database import AsyncSessionLocal
from app.models import MerchantProviderAccount


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        n = await db.scalar(
            select(func.count()).select_from(MerchantProviderAccount).where(MerchantProviderAccount.shop_id.is_not(None))
        )
        print(f"{n} provider account(s) are pinned to a single shop.")
        if apply and n:
            await db.execute(update(MerchantProviderAccount).values(shop_id=None))
            await db.commit()
            print("Done: all provider accounts are now merchant-wide.")
        elif n:
            print("Re-run with --yes to make them merchant-wide.")


if __name__ == "__main__":
    asyncio.run(main("--yes" in sys.argv))
