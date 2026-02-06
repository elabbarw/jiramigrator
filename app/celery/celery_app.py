from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "jira_migration",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.celery.worker"]
)

# Optional configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

if __name__ == "__main__":
    celery_app.start() 