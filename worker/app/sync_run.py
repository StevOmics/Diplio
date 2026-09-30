"""Sync run: portability (docs/backup-plan/steps/18-portability.md,
DEVIATIONS.md D13) - pulling a library's already-existing backup content down
from a bucket that a *different* (or reset) instance wrote to.

This instance's database has no rows for that content, by definition (it's
never run a backup to this destination before), so - unlike a normal
restore - the ledger isn't looked up, it's built from the bucket's own index
files (bucket_inventory.discover, the index-file-fallback mechanism D7
deferred, repurposed here for "never had a row" instead of "database is
down"). Each file that can be recovered gets exactly the MediaFile /
BackupRecord / BackupArchive / BackupRecordArchive rows a normal backup run
would have left, so a future backup of this library finds everything already
stored via _adopt_existing_copies and uploads nothing - this library is fully
"reconnected", not just topped up with local copies.

Tracked on a SyncRun row (created by the web app as "queued"), same
live-progress shape as BackupRun/RestoreRun.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag

from app.archive_paths import is_s3_path, normalize_prefix, parse_gcs_path, parse_s3_path
from app.backup_run import _Progress
from app.bucket_inventory import DiscoveredFile, discover
from app.celery_app import app
from app.db import SessionLocal
from app.encryption import (
    decrypt_paths,
    decrypt_sha256,
    derive_file_key,
    derive_key_check,
    derive_master_key,
    derive_sha256_seal_key,
)
from app.fingerprint import compute_fingerprint
from app.models import (
    BackupArchive,
    BackupEncryptionConfig,
    BackupKeyVersion,
    BackupRecord,
    BackupRecordArchive,
    MediaFile,
    StorageLocation,
    SyncRun,
)
from app.storage import backend_for_destination
from app.tasks import _restore_v2

logger = logging.getLogger(__name__)


def _update_sync_run(db, run_id: int | None, **fields) -> None:
    """Best-effort progress write, like backup_run._update_run: a run's real
    work must not fail because its status row couldn't be updated."""
    if run_id is None:
        return
    try:
        run = db.get(SyncRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        db.commit()
    except Exception:  # pragma: no cover - logged, never fatal
        logger.exception("could not update sync run %s", run_id)
        db.rollback()


def _resolve_prefix(db, library: StorageLocation, archive: StorageLocation) -> tuple[str, str]:
    if is_s3_path(archive.path):
        bucket, base = parse_s3_path(archive.path)
    else:
        bucket, base = parse_gcs_path(archive.path)
    subpath = library.archive_subpath if library.archive_location_id == archive.id else ""
    return bucket, normalize_prefix(base, subpath)


def _all_candidate_keys(db) -> list[tuple[int | None, bytes]]:
    """Every master key this instance knows how to derive, current key first:
    (key_version_id, master_key). A fresh instance recovering someone else's
    backups has exactly one candidate - whatever passphrase(+salt, see
    Settings > Backup Encryption's portability field) the user just entered
    as the *current* key. An instance with its own history also tries every
    retired version, in case the bucket holds archives from an old rotation."""
    keys: list[tuple[int | None, bytes]] = []
    seen: set[tuple[str, str]] = set()
    config = db.query(BackupEncryptionConfig).first()
    if config and config.password and config.kdf_salt:
        current_id = None
        current_row = (
            db.query(BackupKeyVersion)
            .filter_by(password=config.password, kdf_salt=config.kdf_salt, retired_at=None)
            .order_by(BackupKeyVersion.id.desc())
            .first()
        )
        if current_row:
            current_id = current_row.id
        keys.append((current_id, derive_master_key(config.password, bytes.fromhex(config.kdf_salt))))
        seen.add((config.password, config.kdf_salt))
    for row in db.query(BackupKeyVersion):
        if (row.password, row.kdf_salt) in seen:
            continue
        seen.add((row.password, row.kdf_salt))
        keys.append((row.id, derive_master_key(row.password, bytes.fromhex(row.kdf_salt))))
    return keys


class _ContentResolver:
    """bucket_inventory.discover's resolve_sha256 and resolve_paths callbacks,
    combined so both share one cache: "which of our known keys opens this
    archive_id", tried at most once (len(candidate keys) attempts) no matter
    how many fields on how many entries need resolving.

    resolve_sha256 recovers a content-id-keyed entry's real sha256 from its
    sealed "sha256_enc" (docs/backup-plan/DEVIATIONS.md) - required before
    resolve_paths can do anything, since paths_enc's key and AAD are the real
    sha256, not the entry's dict key. A legacy (key_scheme-less) entry's real
    sha256 is already its own dict key; bucket_inventory.discover never calls
    resolve_sha256 for one. A plaintext entry's "paths" needs no key at all.

    Identifying which candidate key opens a given archive prefers the
    index's stored "key_check" (one cheap HMAC per candidate) over
    trial-decrypting a real entry, falling back to the trial-decrypt for an
    archive written before key_check existed - see _find_key_for_archive."""

    def __init__(self, candidate_keys: list[tuple[int | None, bytes]]):
        self._candidate_keys = candidate_keys
        self._key_for_archive: dict[str, tuple[int | None, bytes] | None] = {}

    def key_version_for(self, archive_id: str) -> int | None:
        found = self._key_for_archive.get(archive_id)
        return found[0] if found else None

    def resolve_sha256(self, content_id: str, entry: dict, index: dict) -> str | None:
        archive_id = index["archive_id"]
        token = entry.get("sha256_enc")
        if not token:
            return None
        if archive_id not in self._key_for_archive:
            self._key_for_archive[archive_id] = self._find_key_for_archive(index)
        cached = self._key_for_archive[archive_id]
        if cached is None:
            return None
        _, master_key = cached
        try:
            return decrypt_sha256(derive_sha256_seal_key(master_key), content_id, token)
        except (InvalidTag, ValueError):
            return None  # the archive's cached key doesn't open *this* entry either

    def _find_key_for_archive(self, index: dict) -> tuple[int | None, bytes] | None:
        """Identifies which candidate key opens this archive, preferring the
        stored "key_check" (encryption.derive_key_check) when present - one
        cheap HMAC per candidate instead of a trial AES-GCM decrypt. Falls
        back to trial-decrypting the first entry's sha256_enc for an archive
        written before key_check existed."""
        key_check = index.get("key_check")
        if key_check is not None:
            for key_version_id, master_key in self._candidate_keys:
                if derive_key_check(master_key) == key_check:
                    return (key_version_id, master_key)
            return None
        first_item = next(iter(index.get("files", {}).items()), None)
        if first_item is None or not isinstance(first_item[1], dict):
            return None
        first_content_id, first_entry = first_item
        token = first_entry.get("sha256_enc")
        if not token:
            return None
        for key_version_id, master_key in self._candidate_keys:
            try:
                decrypt_sha256(derive_sha256_seal_key(master_key), first_content_id, token)
            except (InvalidTag, ValueError):
                continue
            return (key_version_id, master_key)
        return None

    def resolve_paths(self, sha256: str, entry: dict, archive_id: str) -> list[str] | None:
        if "paths" in entry:
            return entry["paths"]
        token = entry.get("paths_enc")
        if not token:
            return None
        if archive_id in self._key_for_archive:
            cached = self._key_for_archive[archive_id]
            if cached is None:
                return None
            _, master_key = cached
            try:
                return decrypt_paths(derive_file_key(master_key, sha256), sha256, token)
            except (InvalidTag, ValueError):
                return None  # the archive's cached key doesn't open *this* entry either
        for key_version_id, master_key in self._candidate_keys:
            try:
                paths = decrypt_paths(derive_file_key(master_key, sha256), sha256, token)
            except (InvalidTag, ValueError):
                continue
            self._key_for_archive[archive_id] = (key_version_id, master_key)
            return paths
        self._key_for_archive[archive_id] = None
        return None


def _upsert_archive_rows(
    db, archive: StorageLocation, discovered: list[DiscoveredFile], resolver: _ContentResolver, backend
) -> dict[str, BackupArchive]:
    """One BackupArchive row per distinct archive_id actually referenced by
    files being synced, keyed by archive_id. Skips (and the caller then skips
    every file needing it) an archive_id whose object can no longer be
    stat'd - it passed discover()'s liveness check moments ago, but a bucket
    can change under us."""
    by_id: dict[str, BackupArchive] = {}
    existing = {
        row.archive_id: row
        for row in db.query(BackupArchive).filter(
            BackupArchive.storage_location_id == archive.id,
            BackupArchive.archive_id.in_({p.archive_id for f in discovered for p in f.parts}),
        )
    }
    for f in discovered:
        for part in f.parts:
            if part.archive_id in by_id:
                continue
            if part.archive_id in existing:
                by_id[part.archive_id] = existing[part.archive_id]
                continue
            stat = backend.stat_or_none(part.archive_path)
            if stat is None:
                continue
            row = BackupArchive(
                storage_location_id=archive.id,
                path=part.archive_path,
                size_bytes=stat.size,
                crc32c=stat.crc32c,
                encrypted=part.encrypted,
                archive_type=part.archive_type,
                archive_id=part.archive_id,
                indexed_at=datetime.now(timezone.utc),
                key_version_id=resolver.key_version_for(part.archive_id) if part.encrypted else None,
            )
            db.add(row)
            by_id[part.archive_id] = row
    if by_id:
        db.flush()
    return by_id


def _execute_sync_run(run_id: int, *, backend=None) -> dict:
    db = SessionLocal()
    try:
        run = db.get(SyncRun, run_id)
        if run is None:
            return {"status": "missing"}
        _update_sync_run(db, run_id, status="running", started_at=datetime.now(timezone.utc), heartbeat_at=datetime.now(timezone.utc))
        try:
            return _run(db, run_id, backend)
        except Exception as exc:
            logger.exception("sync run %s failed", run_id)
            db.rollback()
            _update_sync_run(
                db, run_id, status="failed", error_message=str(exc)[:2000], phase=None, detail=None,
                completed_at=datetime.now(timezone.utc),
            )
            return {"status": "failed", "error": str(exc)}
    finally:
        db.close()


def _run(db, run_id: int, backend) -> dict:
    run = db.get(SyncRun, run_id)
    library = db.get(StorageLocation, run.library_storage_location_id)
    archive = db.get(StorageLocation, run.archive_storage_location_id)
    if library is None or archive is None:
        raise RuntimeError("the library or the archive no longer exists")

    if backend is None:
        _, prefix = _resolve_prefix(db, library, archive)
        backend = backend_for_destination(db, archive)
    else:
        _, prefix = _resolve_prefix(db, library, archive)

    _update_sync_run(db, run_id, detail="Listing the bucket")
    resolver = _ContentResolver(_all_candidate_keys(db))
    inventory = discover(backend, prefix, resolver.resolve_paths, resolve_sha256=resolver.resolve_sha256)

    library_root = Path(library.path)
    existing_paths = {mf.path: mf for mf in db.query(MediaFile).filter_by(storage_location_id=library.id)}
    already_synced_shas = {
        r.sha256
        for r in db.query(BackupRecord).filter_by(destination_storage_location_id=archive.id, status="done")
        if r.sha256
    }

    # (DiscoveredFile, target_path) for every path worth attempting - resolved
    # up front so the run's file count (and progress total) is known before
    # any work starts.
    candidates: list[tuple[DiscoveredFile, Path]] = []
    results: list[dict] = []
    for f in inventory.files:
        if f.paths is None:
            for part in f.parts[:1]:
                results.append({"path": f"(sha256 {f.sha256[:12]}, encrypted)", "status": "skipped", "error": "no configured key could decrypt this file's path"})
            continue
        for rel_path in f.paths:
            target = library_root / rel_path
            existing = existing_paths.get(str(target))
            if existing and f.sha256 in already_synced_shas and existing.fingerprint:
                results.append({"path": str(target), "status": "skipped", "error": "already synced"})
                continue
            candidates.append((f, target))

    total_bytes = sum(f.size_bytes for f, _ in candidates)
    _update_sync_run(db, run_id, files_total=len(candidates) + len(results))
    progress = _Progress(db, run_id, update=_update_sync_run)
    progress.phase("syncing", total_bytes)

    synced_bytes = 0
    archive_rows = _upsert_archive_rows(db, archive, [f for f, _ in candidates], resolver, backend)
    # Committed now, before any per-file work: a later per-file failure does
    # db.rollback(), which would otherwise undo these still-unflushed-to-disk
    # inserts too (_upsert_archive_rows only flushes) and leave every *other*
    # file in this run holding a stale, rolled-back BackupArchive reference.
    db.commit()

    for number, (f, target) in enumerate(candidates, 1):
        _update_sync_run(db, run_id, detail=f"Syncing {number}/{len(candidates)}: {target.name}")
        base = synced_bytes
        try:
            missing_archives = [p.archive_id for p in f.parts if p.archive_id not in archive_rows]
            if missing_archives:
                raise RuntimeError(f"archive object for {missing_archives[0]!r} is no longer in the bucket")

            media_file = MediaFile(
                path=str(target),
                filename=target.name,
                extension=target.suffix.lstrip(".").lower(),
                media_type=library.media_type,
                size_bytes=f.size_bytes,
                storage_location_id=library.id,
            )
            db.add(media_file)
            db.flush()

            record = BackupRecord(
                media_file_id=media_file.id,
                destination_storage_location_id=archive.id,
                local_path=str(target),
                sha256=f.sha256,
                compression=f.compression,
                status="done",
            )
            db.add(record)
            db.flush()

            links: list[tuple[BackupRecordArchive, BackupArchive]] = []
            for part_index, part in enumerate(f.parts):
                archive_row = archive_rows[part.archive_id]
                link = BackupRecordArchive(
                    backup_record_id=record.id,
                    backup_archive_id=archive_row.id,
                    part_index=part_index,
                    archive_offset=part.offset,
                    archive_length=part.size,
                )
                db.add(link)
                links.append((link, archive_row))
            db.flush()

            _restore_v2(
                db, media_file, record, links, target, backend,
                on_progress=lambda fetched, base=base: progress.advance(base + fetched),
            )

            media_file.fingerprint = compute_fingerprint(target, target.stat().st_size)
            media_file.size_bytes = target.stat().st_size
            record.mtime_ns = target.stat().st_mtime_ns
            record.local_checksum = media_file.fingerprint
            db.commit()
            results.append({"path": str(target), "status": "synced"})
        except Exception as exc:  # noqa: BLE001 - one file failing must not stop the rest
            logger.warning("sync of %s failed: %s", target, exc)
            db.rollback()
            results.append({"path": str(target), "status": "failed", "error": str(exc)[:300]})
        else:
            synced_bytes += f.size_bytes
        progress.advance(base + f.size_bytes)
        progress.flush()

    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    synced = len(results) - failed - skipped
    status = "done" if failed == 0 else ("failed" if synced == 0 else "partial")
    progress.finish()
    _update_sync_run(
        db, run_id,
        status=status, files_synced=synced, files_failed=failed, files_skipped=skipped, bytes_synced=synced_bytes,
        results_json=json.dumps(results), detail=None, completed_at=datetime.now(timezone.utc),
    )
    return {"status": status, "synced": synced, "failed": failed, "skipped": skipped, "bytes": synced_bytes, "results": results}


@app.task(bind=True, name="sync_run")
def sync_run(self, run_id: int) -> None:
    db = SessionLocal()
    try:
        _update_sync_run(db, run_id, celery_task_id=self.request.id)
    finally:
        db.close()
    _execute_sync_run(run_id)
