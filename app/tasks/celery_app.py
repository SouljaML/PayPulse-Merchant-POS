from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery("paypulse", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.beat_schedule = {
    "expire-stale-transactions": {
        "task": "app.tasks.expiry.expire_stale_transactions_task",
        "schedule": 60.0,  # every minute
    },
    "poll-pending-transactions": {
        "task": "app.tasks.reconciliation.poll_pending_transactions_task",
        "schedule": 120.0,  # every 2 minutes — catches lost callbacks
    },
    "daily-settlement-snapshot": {
        "task": "app.tasks.reconciliation.run_daily_settlement_task",
        "schedule": crontab(hour=23, minute=55),  # just before end of business day
    },
}
celery_app.conf.timezone = "Africa/Maseru"
