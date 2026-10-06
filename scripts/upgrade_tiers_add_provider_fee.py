"""One-off upgrade for databases that already created provider_commission_tiers
from the first version of the tier model (before provider_fee existed).
create_all() only creates MISSING tables, so it won't add the new column.

Safe to run more than once. A fresh database doesn't need this — init_db.py
creates the table with the column already in place.

Usage:
    python scripts/upgrade_tiers_add_provider_fee.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE provider_commission_tiers ADD COLUMN IF NOT EXISTS provider_fee NUMERIC(14, 2) NOT NULL DEFAULT 0")
        )
    print("provider_commission_tiers.provider_fee is in place.")


if __name__ == "__main__":
    asyncio.run(main())
