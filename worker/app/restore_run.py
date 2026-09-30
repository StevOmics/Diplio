"""Restore run: put files back at their original paths from a v2 archive.

Tracked on a RestoreRun row (created by the web app as "queued") so the UI can
show live progress, like BackupRun does for backups. Each file is rebuilt from
its ledger (`tasks._v2_ledger_parts`) by `tasks._restore_v2`, which verifies the
SHA-256 before moving anything into place - a file that fails verification
leaves the existing file untouched. One file failing does not stop the rest:
the run finishes "partial" with a per-file result list.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.backup_run import _Progress
from app.celery_app import app
from app.db import SessionLocal
from app.models import MediaFile, RestoreRun, StorageLocation
from app.notify import notify
from app.storage import StorageBackend, backend_for_destination
from app.tasks import _restore_v2, _v2_ledger_parts

logger = logging.getLogger(__name__)


def _update_restore_run(db, run_id: int | None, **fields) -> None:
    """Best-effort progress write, like backup_run._update_run: a run's real
    work must not fail because its status row couldn't be updated."""
    if run_id is None:
        return
    try:
        run = db.get(RestoreRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        db.commit()
    except Exception:  # pragma: no cover - logged, never fatal
        logger.exception("could not update restore run %s", run_id)
        db.rollback()


def _backend_for(db, destination: StorageLocation) -> StorageBackend:
    return backend_for_destination(db, destination)


def _execute_restore_run(run_id: int, *, backend: StorageBackend | None = None) -> dict:
    db = SessionLocal()
    try:
        run = db.get(RestoreRun, run_id)
        if run is None:
            return {"status": "missing"}
        _update_restore_run(db, run_id, status="running", started_at=datetime.now(timezone.utc), heartbeat_at=datetime.now(timezone.utc))
        try:
            return _run(db, run_id, backend)
        except Exception as exc:
            logger.exception("restore run %s failed", run_id)
            db.rollback()
            _update_restore_run(
                db, run_id, status="failed", error_message=str(exc)[:2000], phase=None, detail=None,
                completed_at=datetime.now(timezone.utc),
            )
            # Cancelled-by-user runs never reach here (revoked/terminated,
            # not raised), so this only fires for genuine failures.
            destination = db.get(StorageLocation, run.destination_storage_location_id) if run else None
            notify(
                db,
                "restore_failed",
                "error",
                f"Restore failed: {destination.name if destination else 'archive'}",
                f"Restore from \"{destination.name if destination else 'archive'}\" failed: {str(exc)[:500]}",
            )
            return {"status": "failed", "error": str(exc)}
    finally:
        db.close()


def _run(db, run_id: int, backend: StorageBackend | None) -> dict:
    run = db.get(RestoreRun, run_id)
    destination = db.get(StorageLocation, run.destination_storage_location_id)
    if destination is None:
        raise RuntimeError("the archive to restore from no longer exists")
    backend = backend or _backend_for(db, destination)

    replace_older = (run.mode or "replace_older") != "replace_all"

    ids = json.loads(run.file_ids)
    results: list[dict] = []
    items = []
    for media_file_id in ids:
        media_file = db.get(MediaFile, media_file_id)
        v2 = _v2_ledger_parts(db, media_file_id, destination.id) if media_file else None
        if media_file is None:
            results.append({"path": f"(file {media_file_id})", "status": "failed", "error": "no longer in the catalog"})
        elif v2 is None:
            results.append({"path": media_file.path, "status": "failed", "error": "no completed backup to restore from"})
        else:
            record = v2[0]
            if replace_older and record.mtime_ns is not None:
                try:
                    local_mtime_ns = Path(media_file.path).stat().st_mtime_ns
                except (FileNotFoundError, NotADirectoryError):
                    local_mtime_ns = None
                if local_mtime_ns is not None and local_mtime_ns >= record.mtime_ns:
                    results.append(
                        {"path": media_file.path, "status": "skipped", "reason": "local file is newer"}
                    )
                    continue
            items.append((media_file, v2[0], v2[1]))
    # Archive order, so a clump's members are fetched near each other.
    items.sort(key=lambda it: (it[2][0][1].id, it[2][0][0].archive_offset))

    total_bytes = sum(mf.size_bytes or 0 for mf, _, _ in items)
    _update_restore_run(db, run_id, files_total=len(ids))
    progress = _Progress(db, run_id, update=_update_restore_run)
    progress.phase("restoring", total_bytes)

    restored_bytes = 0
    for number, (media_file, record, parts) in enumerate(items, 1):
        _update_restore_run(db, run_id, detail=f"Restoring {number}/{len(items)}: {media_file.filename}")
        stored_total = sum(link.archive_length for link, _ in parts) or 1
        size = media_file.size_bytes or 0
        base = restored_bytes
        try:
            _restore_v2(
                db, media_file, record, parts, Path(media_file.path), backend,
                # stored bytes -> the file's own size, so the bar tracks plain bytes
                on_progress=lambda fetched, base=base, size=size, stored=stored_total: progress.advance(
                    base + int(size * min(fetched, stored) / stored)
                ),
            )
            results.append({"path": media_file.path, "status": "restored"})
        except Exception as exc:  # noqa: BLE001 - one file failing must not stop the rest
            logger.warning("restore of %s failed: %s", media_file.path, exc)
            db.rollback()
            results.append({"path": media_file.path, "status": "failed", "error": str(exc)[:300]})
        else:
            restored_bytes += size
        progress.advance(base + size)
        progress.flush()

    failed = sum(1 for r in results if r["status"] == "failed")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    done = len(results) - failed - skipped_count
    status = "done" if failed == 0 else ("failed" if done == 0 else "partial")
    progress.finish()
    _update_restore_run(
        db, run_id,
        status=status, files_done=done, files_failed=failed, bytes_restored=restored_bytes,
        results_json=json.dumps(results), detail=None, completed_at=datetime.now(timezone.utc),
    )
    notify(
        db,
        "restore_failed" if status != "done" else "restore_done",
        "warning" if status != "done" else "info",
        f"Restore {status}: {destination.name}",
        f"Restore from \"{destination.name}\" finished {status}: {done} restored" + (f", {failed} failed" if failed else "") + ".",
    )
    return {"status": status, "restored": done, "failed": failed, "bytes": restored_bytes, "results": results}


@app.task(bind=True, name="restore_run")
def restore_run(self, run_id: int) -> None:
    db = SessionLocal()
    try:
        _update_restore_run(db, run_id, celery_task_id=self.request.id)
    finally:
        db.close()
    _execute_restore_run(run_id)
