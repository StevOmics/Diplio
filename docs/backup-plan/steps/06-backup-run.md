# Step 06: Backup run

- **Phase:** step 6 of 9 (see `docs/backup-plan/PLAN.md`)
- **Spec sections to read:** `docs/cfa-spec.md` §6 (Backup Run — the whole specification for this step), §4 (object layout), §1, §2
- **Depends on:** 01 (settings), 02 (hashing), 03 (packer), 04 (index), 05 (storage)

## Goal
One Celery task performs a complete backup run: lock, hash, pack, upload, verify, index, record, clean up — in exactly §6's order. **This is the first step that changes runtime behaviour.**

## Context
- §6's seven numbered steps are the specification. The **ordering is load-bearing**: the index file is written only after the upload is confirmed, because §6 says an archive with no index file is incomplete and restore ignores it. Getting the order wrong produces an archive that restore trusts but cannot read.
- Everything this step needs already exists and is tested: `fingerprint.hash_file` (02), `packer.pack` (03), `backup_index.build_index` / `index_object_key` (04), `storage.GCSBackend` / `upload_and_confirm` (05).
- v2.0 adds **no new tables** (§6.6): archives and files go in the existing `BackupArchive`, `BackupRecord`, and `BackupRecordArchive`. Step 05 added `BackupArchive.archive_id` / `archive_type` / `crc32c` / `indexed_at`; step 02 added `BackupRecord.sha256` / `mtime_ns`.
- There is **no version history in v2.0** (§8), so `BackupRecord` stays one row per `(media_file_id, destination_storage_location_id)`, upserted — exactly as `_record_backup` does today. Do not add versioning.
- `worker/app/tasks.py` is 978 lines and owns the legacy pipeline. Put this in a **new** `worker/app/backup_run.py` and register the task there; do not grow `tasks.py`.
- No advisory lock exists anywhere in the repo today (`grep -rc advisory` → none).

## Changes

### `worker/app/backup_run.py` (new, ~260 lines)

```python
BACKUP_RUN_LOCK_KEY = 0x4D42524E   # "MBRN"; any stable int, documented

class BackupRunRefused(Exception): ...

@app.task(name="run_backup", queue="copy")
def run_backup(source_storage_location_id: int, destination_storage_location_id: int) -> dict
```

Both location ids are **parameters**, not discovered. MediaBridge has two overlapping notions of "the backup target" (`StorageLocation.is_backup_target` and `CloudStorageConfig.is_backup_target`); resolving which wins is not this step's job. Validate that the destination exists and has `is_backup_target` set, and refuse with a clear message otherwise.

Order of operations, matching §6:

1. **Refuse if unusable**, before touching any file:
   - `BackupEncryptionConfig.enabled` is true → raise `BackupRunRefused` naming the reason. This is `DEVIATIONS.md` D5: the v2 pipeline writes plain tars, and §3/§7 need `tar -xf` and ranged reads at plaintext offsets, so it is mutually exclusive with the legacy encrypted path. Do not silently write plaintext.
   - No `CloudStorageConfig` with `service_account_json` and `bucket_name` → refuse.
   - Destination is not a backup target → refuse.
2. **Take the advisory lock** (§6: "One run at a time, enforced with a PostgreSQL advisory lock"). Use a **dedicated connection** held for the whole run:
   ```python
   lock_conn = engine.connect()
   got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": BACKUP_RUN_LOCK_KEY}).scalar()
   ```
   If `got` is false, return immediately with `{"status": "skipped", "reason": "another run holds the lock"}` — not an exception; a scheduled run colliding with a manual one is normal. Release with `pg_advisory_unlock` in a `finally`, then close the connection.
   **It must be a dedicated connection, not the ORM session's.** Advisory locks are session-scoped, so if SQLAlchemy returns the session's connection to the pool mid-run the lock leaks onto a pooled connection and every later run is blocked until the worker restarts.
3. **Build the file list** from the catalog: `MediaFile` rows with `storage_location_id == source_storage_location_id`. The catalog is already populated by `catalog.scan_library`, so this step performs **no filesystem walk** — that is step 8's business. Apply `StorageLocation.exclude_globs` (step 01, newline-separated) with `fnmatch.fnmatch` against each file's path relative to the source root; note in a comment that `fnmatch`'s `*` crosses `/`, which is why `**/.cache/**` behaves as intended.
4. **Hash** each file with `fingerprint.hash_file`. On `FileChangedDuringRead`, log it, skip the file, and mark the run `partial` (§6.2 — no retry in v2.0). A missing file is the same: log, skip, `partial`.
5. **Pack** with `packer.pack`, passing `min_size_bytes` / `clump_size_bytes` / `max_size_bytes` read from `TransferConfig` (step 01), into a `tempfile.TemporaryDirectory(prefix="mb-backup-")`.
6. **For each `PackedArchive`, in this order and no other:**
   1. `upload_and_confirm(backend, archive_object_key(...), archive.local_path)` → raises after 5 attempts (step 05).
   2. `backend.write_bytes(index_object_key(...), json.dumps(build_index(...)).encode())` — **only after** the upload is confirmed.
   3. Record rows in one transaction: `BackupArchive` (`archive_id`, `archive_type`, `path` = the object key, `size_bytes`, `crc32c` from the confirmed `ObjectStat`, `indexed_at` = now, `storage_location_id` = destination, `encrypted=False`, `is_clump` = `archive_type == "clump"`), then per member a `BackupRecord` upsert and its `BackupRecordArchive` link.
   4. Delete the local tar (§6.7), so disk use stays bounded by one archive rather than the whole run.
7. Return a summary dict: `{"status": "ok"|"partial", "archives": n, "files": n, "skipped": [...], "bytes_uploaded": n}`.

**`BackupRecordArchive.archive_length` for a part — the one real gap.** `PackedMember.size_bytes` is the *whole file's* size even on a part member (§5 keys entries by the whole file's hash), so it is **not** the member's byte length in that tar. For clump and single members the two coincide; for a part they do not. Recover the part's own length with `packer.split_part_sizes(member.size_bytes, max_size=max_size_bytes)[member.part - 1]`, which is deterministic and already tested. Do not store the whole-file size as `archive_length` — step 07's restore reads that value as a byte count.

`BackupRecord` upsert: set `local_path`, `sha256`, `mtime_ns`, `status="done"`, clear `verify_status`/`verified_at`, and keep writing the existing `local_checksum` fingerprint so the legacy drift comparison keeps working. Replace that record's `BackupRecordArchive` rows rather than appending, as `_record_backup` does.

### `worker/app/celery_app.py`
Register the new module so the task is discovered. Follow whatever include/import mechanism is already there.

## Do Not Touch
- `worker/app/tasks.py` — every existing task, `_record_backup`, `_plan_backup`'s callers, the legacy restore functions. The old pipeline must keep working unchanged; step 08 retires the old planner.
- `worker/app/gcs.py`, `worker/app/encryption.py`, `worker/app/packer.py`, `worker/app/backup_index.py`, `worker/app/storage.py`.
- `web/app/catalog.py`. No scanning in this step.
- Any model. The schema for this step already landed in 02 and 05.
- No UI. Triggering this from the web app is step 08's concern.

## Tests to Write First
`worker/tests/test_backup_run.py` (new). Marked `db` where a database is needed; everything storage-related uses `LocalBackend` from `worker/tests/storage_double.py`. **No test may contact a real bucket.**

Refusals (no database writes, no files touched):
- `test_refuses_when_encryption_enabled` (D5): asserts `BackupRunRefused` and that nothing was uploaded
- `test_refuses_without_cloud_config`
- `test_refuses_when_destination_is_not_a_backup_target`

Locking:
- `test_second_run_skips_while_lock_held`: returns `status="skipped"`, uploads nothing
- `test_lock_released_on_success` and `test_lock_released_on_failure`: a later run acquires it
- `test_lock_uses_a_dedicated_connection`: the ORM session's connection being returned to the pool does not release the lock

Ordering — the most valuable tests here:
- `test_index_written_only_after_upload_confirmed`: with a backend that records call order, assert the index `write_bytes` for an archive never precedes its confirmed upload
- `test_failed_upload_writes_no_index_and_no_rows`: `FlakyBackend` failing all attempts leaves no index object and no `BackupArchive` row — the §6 "incomplete archive" guarantee
- `test_rows_recorded_only_after_index`: `indexed_at` is non-null on every recorded archive

Content:
- `test_small_files_clumped_and_recorded`: one `BackupArchive`, one `BackupRecord` per file, correct `BackupRecordArchive` offsets
- `test_part_archive_length_is_part_size_not_whole_file`: for a split file, each `BackupRecordArchive.archive_length` equals that part's byte count, and their sum equals the file size. **This is the gap called out above; it is the test most likely to catch a wrong implementation.**
- `test_excluded_files_are_skipped`
- `test_changed_during_read_file_is_skipped_and_run_is_partial` (§6.2)
- `test_temp_files_deleted`: the temp directory is empty of tars afterwards
- `test_backup_record_keeps_legacy_checksum`: `local_checksum` still holds the fingerprint

## Commands
- Run: `docker compose run --rm -e MEDIABRIDGE_TEST_DB=1 worker pytest -x -q tests/test_backup_run.py`
- Full suites: `cd worker && pytest -x -q` then `cd web && pytest -x -q`
- The pure-logic suites must still pass with nothing running.

## Done When
- [ ] All listed tests exist and pass
- [ ] Both full suites pass; the default (no-services) run is still green
- [ ] No test contacts a real bucket
- [ ] `git diff` shows `worker/app/tasks.py` unchanged
- [ ] The legacy backup and restore paths still work
- [ ] A row added at the top of `docs/CHANGES.md`
- [ ] If anything departs from the spec, an entry added to `docs/backup-plan/DEVIATIONS.md`

## Notes for the Implementer
- **§6's order is the whole point of this step.** Upload, confirm, index, record, delete. If a refactor makes the order implicit or hard to see, undo it.
- The advisory lock on a pooled connection is a genuine trap: get it wrong and the failure is a permanently stuck backup that only a worker restart clears, long after the run that caused it.
- `archive_length` for a part is the other trap, and the two are the reason this step is specified this tightly.
- A crash between the index write and the row recording leaves an index with no database rows. That is survivable — §7's restore falls back to reading index files — so do not add a recovery mechanism for it here.
- Keep `run_backup` readable as §6's seven steps. Extract helpers freely, but the task body should read like the spec.
