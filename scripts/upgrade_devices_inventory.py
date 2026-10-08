"""Upgrade the devices table to the PayPulse-owned fleet model.
Safe to run more than once. Fresh databases don't need it.

Usage:  python scripts/upgrade_devices_inventory.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.database import engine

STATEMENTS = [
    "ALTER TABLE devices ALTER COLUMN merchant_id DROP NOT NULL",
    "ALTER TABLE devices ALTER COLUMN shop_id DROP NOT NULL",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS till_id UUID",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS serial_number VARCHAR(100)",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS suspended_reason VARCHAR(500)",
    "UPDATE devices SET assigned_at = created_at WHERE assigned_at IS NULL AND merchant_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_serial ON devices (serial_number) WHERE serial_number IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_till ON devices (till_id) WHERE till_id IS NOT NULL",
]


async def main() -> None:
    async with engine.connect() as conn:
        ac = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await ac.execute(text("ALTER TYPE device_status ADD VALUE IF NOT EXISTS 'SUSPENDED'"))
        for stmt in STATEMENTS:
            await ac.execute(text(stmt))
    print("devices table upgraded.")


if __name__ == "__main__":
    asyncio.run(main())
