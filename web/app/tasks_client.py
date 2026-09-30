"""
A lightweight Celery client for enqueueing tasks that the worker/celery
services execute (app/tasks.py in ../worker). Only used to send tasks by
name - never imports or runs the worker's task code.
"""
from celery import Celery

from app.config import settings

celery_client = Celery("mediabridge", broker=settings.broker_url)


def enqueue_copy_job(job_id: int) -> str:
    result = celery_client.send_task("copy_media_file", args=[job_id], queue="copy")
    return result.id


def enqueue_verify_job(job_id: int) -> str:
    result = celery_client.send_task("verify_copy_job", args=[job_id], queue="copy")
    return result.id


def enqueue_restore_run(run_id: int) -> str:
    """v2 restore (worker/app/restore_run.py) on the copy queue; run_id is the
    RestoreRun row the UI tracks."""
    result = celery_client.send_task("restore_run", args=[run_id], queue="copy")
    return result.id


def enqueue_sync_run(run_id: int) -> str:
    """Portability sync (worker/app/sync_run.py) on the copy queue; run_id is
    the SyncRun row the UI tracks."""
    result = celery_client.send_task("sync_run", args=[run_id], queue="copy")
    return result.id


# Files per verify task: big enough that a clump's members are checked together
# (cost tracks archives, not files), small enough that one task stays short.
VERIFY_BATCH_SIZE = 500


def enqueue_verify_batches(media_file_ids: list[int], destination_storage_location_id: int, deep: bool = False) -> int:
    """Queues verifies for many files at one archive as batched tasks; returns
    how many tasks were queued."""
    tasks = 0
    for i in range(0, len(media_file_ids), VERIFY_BATCH_SIZE):
        celery_client.send_task(
            "verify_v2_batch",
            args=[media_file_ids[i : i + VERIFY_BATCH_SIZE], destination_storage_location_id, deep],
            queue="copy",
        )
        tasks += 1
    return tasks


def enqueue_clump_backup(job_ids: list[int]) -> str:
    result = celery_client.send_task("backup_clump", args=[job_ids], queue="copy")
    return result.id


def enqueue_run_backup(
    source_storage_location_id: int,
    destination_storage_location_id: int,
    file_ids: list[int] | None = None,
    run_id: int | None = None,
) -> str:
    """v2 backup run (worker/app/backup_run.py) on the copy queue. file_ids
    None = whole library; run_id is the BackupRun row the UI tracks. One run
    at a time is enforced by the worker's advisory lock (others wait)."""
    result = celery_client.send_task(
        "run_backup",
        args=[source_storage_location_id, destination_storage_location_id, file_ids, run_id],
        queue="copy",
    )
    return result.id
