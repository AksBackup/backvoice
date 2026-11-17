from celery import Celery

from app.config import get_settings

s = get_settings()
celery = Celery("astric_voice", broker=s.REDIS_URL, include=["app.dialer.tasks"])
celery.conf.update(
    task_serializer="json", accept_content=["json"], timezone="UTC", enable_utc=True,
    worker_prefetch_multiplier=1, task_acks_late=True, broker_connection_retry_on_startup=True,
    beat_schedule={
        "dialer-tick": {"task": "voice.tick", "schedule": float(s.DIALER_TICK_SECONDS)},
        "stuck-call-reaper": {"task": "voice.reap", "schedule": 120.0},
    },
)
