from celery import Celery

from app.config import settings

app = Celery(
    "mediabridge",
    broker=settings.broker_url,
    backend="rpc://",
    include=["app.tasks", "app.backup_run", "app.restore_run", "app.sync_run", "app.scheduler"],
)
# Everything runs on one queue, consumed by the `worker` service: backup runs,
# restore runs and verifies are all long-running file/bucket work.
app.conf.task_default_queue = "copy"
app.conf.task_routes = {
    "run_backup": {"queue": "copy"},
    "restore_run": {"queue": "copy"},
    "sync_run": {"queue": "copy"},
    "verify_v2_batch": {"queue": "copy"},
    "verify_v2_backup": {"queue": "copy"},
    "check_due_schedules": {"queue": "copy"},
}
# Scheduled backups (docs/CHANGES.md): a single static beat entry checks once
# a minute for any BackupSchedule that's due, rather than a dynamic per-schedule
# beat entry - see app.scheduler.check_due_schedules. Requires the `beat`
# service (docker-compose.yml) to be running.
app.conf.beat_schedule = {
    "check-due-backup-schedules": {
        "task": "check_due_schedules",
        "schedule": 60.0,
    },
}
