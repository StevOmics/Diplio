# Step 07: Restore

- **Phase:** step 7 of 9 (see `docs/backup-plan/PLAN.md`)
- **Spec sections to read:** `docs/cfa-spec.md` §7 (Restore - the whole specification for this step), §5 (index file - `size` is the whole file even for a part), §3 (archive types), §6.6 (no new tables)
- **Depends on:** 06 (backup run - this step reads the rows it writes)

## Goal
Restoring a file backed up by the v2 pipeline: look it up in the database, fetch its archive(s), reconstruct it (concatenating parts in order for a split file), verify its SHA-256, and only then move it into place. Extends the existing restore path in `worker/app/tasks.py` rather than adding a new module - see PLAN.md's Conventions.

## Context
- `worker/app/tasks.py`'s `copy_media_file` task already handles `job.job_type == "restore"` for the **legacy** format, via `_ledger_parts` (frozen) → `_restore_from_parts` (frozen), with a further fallback to `CopyJob`'s own log for backups made before the ledger existed. All three stay untouched and keep working exactly as today - see "Do Not Touch".
- v2 archives and legacy archives live in the **same** `BackupArchive`/`BackupRecord`/`BackupRecordArchive` tables (§6.6, no new tables). They're told apart by `BackupArchive.archive_id`: `NULL` on a legacy row, set (a UUID4 string) on every v2 row - see the comment on that column in `models.py`. So this step adds its own lookup (a v2 equivalent of `_ledger_parts`) rather than changing the existing one, and `copy_media_file`'s restore branch tries the v2 lookup first, falling back to the untouched legacy chain when it finds nothing.
- **An archive with `indexed_at IS NULL` is incomplete and is skipped** (§6, last line) - the v2 lookup filters it out. In practice `run_backup` (step 06) always sets `indexed_at` in the same transaction that creates the row, so this should never actually be null; the filter is spec-mandated defense against a future writer that doesn't hold that invariant, not a case this step needs to construct a repro for.
- **Reading a member's bytes differs by archive type, and this is the load-bearing design decision for this step:**
  - **clump / single:** step 06 already stores the member's exact byte range in the database - `BackupRecordArchive.archive_offset` / `.archive_length`. A tar stores each member's raw data contiguously starting at its header's data offset, so a ranged read of exactly `[offset, offset + length)` **is** the plain file's bytes - no tar parsing needed, and (for a clump) no need to download the other members sharing that archive.
  - **part:** §5's index carries no member length - `size` is the *whole file's* size on a part entry, not that part's byte length (see the comment in `backup_index.py`, and D-something in `DEVIATIONS.md` if this gets logged as one). A part archive holds exactly one member, so the simplest correct approach - and the one this step uses - is to download the whole object and let `tarfile`'s own header parsing determine the exact payload length, rather than trusting `archive_length` or the index's `size` for the byte count. (Step 06 *does* store the correct part length in `archive_length` for traceability, but restore does not depend on it being right - `tarfile` is the source of truth here.)
- **GCS only, unconditionally**, matching step 06: v2 backup never checks `destination.location_type`, it just requires a usable `CloudStorageConfig` and always builds a `GCSBackend`. Restore does the same - no local-destination v2 case exists yet.
- Verify-before-place: reconstruct into a temp file (`<output_path>.mbcopy`, matching the legacy convention already in `_run_plain_copy`/the legacy GCS restore branch), hash it incrementally while writing, compare to `BackupRecord.sha256`, and only `Path.replace()` it into `output_path` if they match. A mismatch must leave `output_path` untouched and clean up the temp file.
- Peak memory: `StorageBackend.read_range`/`.download` return/produce whole byte ranges, not streams (that was step 05's design, not this step's to revisit), so one member's bytes (up to `max_size - 1 MiB`) are unavoidably held in memory at once. What this step *does* control is not holding the **whole reconstructed file** in memory for a multi-part split file - write each part's bytes to the temp file as soon as they're fetched, hashing incrementally, rather than joining a list of part byte-strings first.

## Changes

### `worker/app/tasks.py` (extend, not a new module)

New functions, placed with the other restore helpers (near `_restore_from_parts`):

```python
def _v2_ledger_parts(db, media_file_id: int, destination_storage_location_id: int) -> tuple[BackupRecord, list[tuple[BackupRecordArchive, BackupArchive]]] | None
def _restore_v2_member_bytes(backend, archive: BackupArchive, link: BackupRecordArchive, tmp_dir: Path) -> bytes
def _restore_v2(db, job: CopyJob, media_file: MediaFile, record: BackupRecord, parts: list[tuple[BackupRecordArchive, BackupArchive]], output_path: Path, backend) -> None
```

`_v2_ledger_parts` mirrors `_ledger_parts`'s shape (same join, same `order_by(part_index)`) but filters to `BackupArchive.archive_id.isnot(None)` and `.indexed_at.isnot(None)`, and returns `None` (not `[]`) when there's nothing to restore from, so the call site reads as a single `if`. Returns the `BackupRecord` alongside the rows because `_restore_v2` needs `record.sha256` to verify against.

`_restore_v2_member_bytes` is the one-function summary of the "Context" section above: `archive.archive_type == "part"` downloads the whole object to `tmp_dir` and extracts its single tar member via `tarfile`; anything else does `backend.read_range(archive.path, link.archive_offset, link.archive_length)`.

`_restore_v2`: open `<output_path>.mbcopy` for writing, iterate `parts` in the order `_v2_ledger_parts` already sorted them (`part_index` ascending - for a clump/single this is one iteration), write each member's bytes and update a running `hashlib.sha256()` as they arrive, commit `job.progress_bytes` per part. After the loop, compare the digest to `record.sha256`; raise (and delete the `.mbcopy` file) on mismatch or if `record.sha256` is `None` - never treat a missing hash to check against as "nothing to verify". On success, `Path.replace()` the temp file into `output_path` and set `job.destination_path`/`job.total_bytes`/`job.progress_bytes`.

### `copy_media_file`'s restore branch

Before the existing `parts = _ledger_parts(...)` line, try the v2 lookup first:

```python
elif job.job_type == "restore":
    output_path = Path(media_file.path)
    v2 = _v2_ledger_parts(db, job.media_file_id, other_location.id)
    if v2:
        record, parts = v2
        cloud_config = _get_cloud_storage_config(db)
        if not cloud_config or not cloud_config.service_account_json or not cloud_config.bucket_name:
            raise RuntimeError("cloud storage isn't configured")
        backend = GCSBackend(cloud_config.service_account_json, cloud_config.bucket_name, cloud_config.project_id)
        _restore_v2(db, job, media_file, record, parts, output_path, backend)
    else:
        parts = _ledger_parts(db, job.media_file_id, other_location.id)
        ... # unchanged from here down
```

New imports: `hashlib`, `tarfile`, `tempfile`, `from app.storage import GCSBackend`.

## Do Not Touch
- `_ledger_parts`, `_restore_from_parts`, `_stage_gcs_encrypted_backup`, `_materialize_archive`, and the legacy-fallback branch inside `copy_media_file` (the `existing_backup = _latest_done_backup(...)` block and everything under it). All frozen since step 01 - new format, new functions.
- `worker/app/packer.py`, `backup_index.py`, `storage.py`, `backup_run.py`. Restore reads what they produce; it doesn't change how they produce it.
- `verify_copy_job` - re-verifying a v2-backed file is a separate, not-yet-scoped gap (note it in `docs/TODO.md` if it isn't already, don't build it here).
- Any model, any web route/template. No UI trigger changes - `_queue_copy(..., job_type="restore")` already works generically; this step only changes what happens once that job reaches the worker.

## Tests to Write First
`worker/tests/test_restore_v2.py` (new). All DB-backed (`MEDIABRIDGE_TEST_DB=1`), reusing `worker/tests/conftest.py`'s `db_session`/`catalog`/`*_config_state` fixtures from step 06. No test contacts a real bucket - use `LocalBackend`. Several tests should produce their fixture data by actually calling step 06's `_execute_backup_run` against a `LocalBackend`, then restoring from that same backend - a real round trip is a stronger test than hand-built rows.

- `test_restores_file_backed_up_as_clump`: two small files backed up together into one clump; restore one, assert the output is byte-identical and the other file in the clump is untouched.
- `test_restores_single_archive`: one file sized between `min_size` and `max_size`; backup then restore; byte-identical.
- `test_restores_split_file_reassembling_parts_in_order`: a file large enough to split into 2+ parts (as in step 06's own part test); backup then restore; byte-identical. This is the test most likely to catch a wrong implementation, the same way step 06's part-length test was for the write side.
- `test_restore_rejects_content_that_fails_sha256_verification`: after a successful v2 backup, flip a byte in the backend's stored archive object (direct filesystem write under the `LocalBackend` root, simulating undetected bucket-side corruption), then assert `_restore_v2` raises, the real `output_path` is left untouched (or, if it pre-existed with old content, unchanged), and no `.mbcopy` file is left behind.
- `test_v2_ledger_parts_ignores_incomplete_archive`: a `BackupArchive` row with `archive_id` set but `indexed_at IS NULL`, linked via `BackupRecordArchive` to a `BackupRecord` - assert `_v2_ledger_parts` returns `None`.
- `test_v2_ledger_parts_returns_none_for_legacy_only_backup`: a `BackupRecord`/`BackupRecordArchive`/`BackupArchive` row set with `archive_id IS NULL` (the legacy shape) - assert `_v2_ledger_parts` returns `None`, proving it won't misread a legacy row as v2.
- `test_copy_media_file_restore_prefers_v2_over_legacy`: one integration-shaped test calling the real `copy_media_file` task (via `.run(job_id)`, no broker needed) end to end for a v2-backed file, proving the wiring - not just the extracted helpers - actually takes the v2 branch and produces `job.status == "done"`.

## Commands
- Run: `docker compose run --rm -e MEDIABRIDGE_TEST_DB=1 -v "$(pwd)/worker:/app" worker sh -c "pip install -q pytest==8.3.4 && pytest -x -q tests/test_restore_v2.py"` (see step 06's commit for why the bind-mount is needed - the image has no `tests/` or dev dependencies baked in)
- Full suites: `cd worker && pytest -x -q` then `cd web && pytest -x -q`
- The pure-logic suites must still pass with nothing running.

## Done When
- [ ] All listed tests exist and pass
- [ ] Both full suites pass; the default (no-services) run is still green
- [ ] No test contacts a real bucket
- [ ] `git diff` shows only `worker/app/tasks.py` changed under `worker/app/` (plus the new test file)
- [ ] The legacy backup and restore paths still work (untouched code, but confirm no accidental edit)
- [ ] A row added at the top of `docs/CHANGES.md`
- [ ] If anything departs from the spec, an entry added to `docs/backup-plan/DEVIATIONS.md`

## Notes for the Implementer
- The part-archive special case (download whole, let `tarfile` find the length) is the crux of this step, the same way the archive_length gap was step 06's. Don't try to be clever and derive a byte range for a part from `archive_length` instead - that value exists for traceability, not because restore needs it.
- A mismatch on SHA-256 verification is not a "partial" outcome to log and move past - it means the stored bytes don't match what was backed up, which is exactly the class of bug this whole pipeline exists to catch. Raise.
- If you find yourself wanting to touch `_ledger_parts` or `_restore_from_parts` to "share more code" with the new functions, don't - the point of freezing them is that the legacy format's restore path is provably unchanged by this work.
