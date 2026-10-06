"""One-off local dev helper: creates all tables from app/models.py against
DATABASE_URL. Not a substitute for Alembic migrations — see README.

Usage:
    python scripts/init_db.py
"""

import asyncio
import sys
from pathlib import Path

# Make the project root importable regardless of the working directory this is
# run from — `python scripts/init_db.py` only puts scripts/ on sys.path by
# default, not the project root where the app/ package lives.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import engine
from app.models import Base


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created.")


if __name__ == "__main__":
    asyncio.run(main())
