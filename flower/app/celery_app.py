from celery import Celery

from app.config import settings

app = Celery("mediabridge", broker=settings.broker_url)
