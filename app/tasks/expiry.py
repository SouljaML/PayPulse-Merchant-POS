import asyncio

from app.database import AsyncSessionLocal
from app.services.transaction_service import expire_stale_transactions
from app.tasks.celery_app import celery_app


async def _run() -> int:
    async with AsyncSessionLocal() as db:
        return await expire_stale_transactions(db)


@celery_app.task(name="app.tasks.expiry.expire_stale_transactions_task")
def expire_stale_transactions_task() -> int:
    return asyncio.run(_run())
