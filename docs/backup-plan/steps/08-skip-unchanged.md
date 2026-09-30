# Step 08: Skip unchanged files

- **Phase:** step 8 of 9 (see `docs/backup-plan/PLAN.md`)
- **Spec sections to read:** `docs/cfa-spec.md` §6.1 (the whole specification for this step), §6 (for context - this slots in as the first of the seven steps)
- **Depends on:** 06 (backup run - `BackupRecord.mtime_ns`, written there, is what this step reads), 07 (restore, no direct dependency, but confirms the ledger shape this step trusts)

## Goal
`run_backup` skips re-hashing, re-packing, and re-uploading a file whose path, size, and mtime already match its last successful v2 backup at this destination (§6.1) - the "unchanged" case that should be the common one on every run after the first.

## Scope decision (2026-09-18, Steve's call)
§6's own step list also names retiring the old percentage-based planner
(`_plan_backup`/`_execute_backup_plan` in `web/app/main.py`) as part of "step
8". That planner drives the UI's **per-file** bulk-backup buttons
(`/movies/bulk-backup`, `/movies/bulk-backup-cloud`) - a different shape of
work entirely from `run_backup`'s **whole-library** runs, and retiring it
means deciding how (or whether) the UI exposes triggering a `run_backup` run
at all. That is a real design decision, not an implementation detail, and is
explicitly **deferred out of this step** - see `DEVIATIONS.md` D8. This step
is scoped to §6.1 only: the skip-check inside `worker/app/backup_run.py`.
`_plan_backup` is untouched and the legacy per-file UI keeps working exactly
as today.

## Context
- No new column. `BackupRecord.mtime_ns` (written by step 06) is the recorded
  mtime; `MediaFile.size_bytes` (kept current by `catalog.scan_library`, a
  web-side concern this step doesn't touch) is the recorded size. The skip
  check is: `current_stat.st_mtime_ns == record.mtime_ns and current_stat.st_size == media_file.size_bytes`.
  **Both** must match - a classic rsync-style quick check, so a change that
  moves only one of the two (e.g. a `touch` with no content change, or an
  edit that happens to preserve mtime) still triggers a re-backup.
- "The last successful backup" means a `BackupRecord` for this
  `(media_file_id, destination_storage_location_id)` pair with a non-null
  `mtime_ns` - null means the row predates step 06 (a legacy-format record
  reused for the same key) and must not be trusted as a v2 "already backed
  up" signal.
- This is a per-file check inside the same catalog read `_build_file_list`
  already does (§6 steps 1-2) - no new filesystem walk, no new Celery task.
  Fetch every relevant `BackupRecord` for the destination in one query before
  the loop (keyed by `media_file_id`), not one query per file - a real
  library can have thousands of rows and this runs on every backup.
- An unchanged file is a skip reason like `"excluded"`, not like `"missing"`
  or `"changed_during_read"` - it must not flip the run's status to
  `"partial"`. It *is* expected, common, and the entire point of this step.

## Changes

### `worker/app/backup_run.py`

`_build_file_list` gains a `destination_storage_location_id: int` parameter.
Before hashing each candidate (after the exclude-glob check, using the same
`Path.stat()` call that already determines whether the file is missing),
look up its `BackupRecord` (from a dict built once, before the loop) and skip
with `{"path": ..., "reason": "unchanged"}` when both quick-check fields
match. `_run_locked` passes `destination.id` through.

`partial`'s definition changes from `any(s["reason"] != "excluded" for s in skipped)`
to excluding `"unchanged"` too: `any(s["reason"] not in ("excluded", "unchanged") for s in skipped)`.

## Do Not Touch
- `web/app/main.py`'s `_plan_backup`, `_execute_backup_plan`, `BackupPlanItem`,
  and the `/movies/bulk-backup*` routes - untouched per the scope decision above.
- `worker/app/tasks.py` - nothing here needs restore-side changes.
- Any model. No schema change this step.
- `packer.py`, `backup_index.py`, `storage.py` - unaffected; a skipped file
  never reaches packing.

## Tests to Write First
Extend `worker/tests/test_backup_run.py` (same module under test, not a new file).

- `test_unchanged_file_is_skipped_and_not_reuploaded`: back up a file once;
  run again with nothing touched; assert the second run's `skipped` contains
  `{"path": ..., "reason": "unchanged"}`, `archives == 0` (no new archive
  needed), and `status == "ok"` (not `"partial"`).
- `test_changed_content_is_backed_up_again`: back up a file, then overwrite
  it with different content and a later mtime (as a real edit would); assert
  the second run re-hashes and re-uploads it - a new `BackupArchive` exists
  and `BackupRecord.sha256`/`mtime_ns` reflect the new content.
- `test_mismatched_size_alone_forces_rebackup`: back up a file, then replace
  its content with different content of a **different length**, but reset
  its mtime back to exactly `record.mtime_ns` with `os.utime` (simulating an
  edit that happens to preserve mtime) and update the catalog's
  `MediaFile.size_bytes` to match the new size. Assert this is **not**
  skipped - the size half of the quick check alone must be enough to force a
  re-backup. This is the test most likely to catch an `or`-instead-of-`and`
  bug in the two-field comparison.
- `test_first_backup_of_a_file_is_never_skipped`: no prior `BackupRecord`
  exists - assert the file is backed up normally (sanity check that the skip
  path only activates when there's something to compare against).

## Commands
- Run: `docker compose run --rm -e MEDIABRIDGE_TEST_DB=1 -v "$(pwd)/worker:/app" worker sh -c "pip install -q pytest==8.3.4 && pytest -x -q tests/test_backup_run.py"`
- Full suites: `cd worker && pytest -x -q` then `cd web && pytest -x -q`

## Done When
- [ ] All listed tests exist and pass
- [ ] Both full suites pass; the default (no-services) run is still green
- [ ] `git diff` touches only `worker/app/backup_run.py` under `worker/app/`, plus the test file
- [ ] `web/app/main.py` is unchanged
- [ ] A row added at the top of `docs/CHANGES.md`
- [ ] `docs/backup-plan/DEVIATIONS.md` gains D8 for the deferred `_plan_backup` retirement

## Notes for the Implementer
- Fetch `BackupRecord`s for the destination once, before the per-file loop -
  not once per file. This step exists partly *because* re-hashing everything
  every run doesn't scale; re-querying the database per file the same way
  would just move the waste rather than remove it.
- Don't be tempted to also compare `local_checksum` (the legacy partial
  BLAKE2b fingerprint) instead of, or in addition to, size+mtime - §6.1 is
  explicit that the check is path/size/mtime, and the whole reason step 02
  built streaming SHA-256 separately was to avoid needing to touch file
  content at all for this decision.
