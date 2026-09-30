"""Periodic backup scheduling (docs/CHANGES.md: scheduled backups). One
Celery Beat entry ("check-due-backup-schedules", every 60s - see
app.celery_app) fires check_due_schedules, which finds any BackupSchedule
that's due and enqueues it through the same run_backup path a manual
"Back up" click uses - see web/app/main.py's _start_backup_run for the
UI-triggered equivalent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.backup_run import run_backup
from app.celery_app import app
from app.db import SessionLocal
from app.models import BackupRun, BackupSchedule, StorageLocation
from app.schedule_utils import compute_next_run

logger = logging.getLogger(__name__)


@app.task(name="check_due_schedules")
def check_due_schedules() -> dict:
    now = datetime.now(timezone.utc)
    fired = 0
    db = SessionLocal()
    try:
        due = (
            db.query(BackupSchedule)
            .filter(BackupSchedule.enabled.is_(True))
            .filter(BackupSchedule.next_run_at.isnot(None))
            .filter(BackupSchedule.next_run_at <= now)
            .all()
        )
        for schedule in due:
            library = db.get(StorageLocation, schedule.source_storage_location_id)
            if not library or not library.archive_location_id:
                logger.warning(
                    "Skipping schedule %s: library %s has no archive assigned",
                    schedule.id,
                    schedule.source_storage_location_id,
                )
                schedule.last_run_at = now
                schedule.next_run_at = compute_next_run(schedule.frequency, schedule.time_of_day, schedule.days_of_week, now)
                continue

            run = BackupRun(
                scope="library",
                source_storage_location_id=library.id,
                destination_storage_location_id=library.archive_location_id,
                status="queued",
            )
            db.add(run)
            db.flush()
            run.celery_task_id = run_backup.delay(library.id, library.archive_location_id, None, run.id).id

            schedule.last_run_at = now
            schedule.next_run_at = compute_next_run(schedule.frequency, schedule.time_of_day, schedule.days_of_week, now)
            fired += 1
        db.commit()
    finally:
        db.close()
    return {"fired": fired}
