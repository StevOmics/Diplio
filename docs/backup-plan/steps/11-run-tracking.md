# Step 11: Run tracking, file-subset runs, test set

- **Depends on:** 06 (run), 10 (encryption). Resolves most of `DEVIATIONS.md` D8.

## What exists
- `BackupRun` (web + worker `models.py`): one row per v2 run. `status` queued -> running -> done | partial | failed; counters `files_total/files_skipped/archives_total/archives_done/bytes_uploaded`; `detail` (phase text), `error_message`, `skipped_json`.
- `worker/app/backup_run.py`: `run_backup(source, dest, file_ids=None, run_id=None)`. `_update_run` writes progress and never raises. `_execute_backup_run` records refusals/crashes as `failed` then re-raises. Lock busy -> run stays `queued` ("Waiting for another backup run"), Celery retries every 15s (max 480).
- `web/app/main.py`: `_start_backup_run` creates the row then enqueues (`tasks_client.enqueue_run_backup`). Used by the library, single-file and checked-files cloud buttons; a selection spanning libraries becomes one run per library.
- Copy Jobs page: "Backup Runs" table, reloads every 5s while any run is queued/running.
- `scripts/make-test-media.py`: deterministic test set (tiny/mid/large + duplicates + odd names); `--clean` only deletes folders carrying its marker file.

## Traps
- `run_backup` positional args are `[source, dest, file_ids, run_id]` - keep web's `enqueue_run_backup` and the worker task in step.
- Explicit selections still go through skip-unchanged, so re-backing up an unchanged file reports it as skipped rather than uploading it again.
- `BLOB_CHUNK_SIZE` is only an upper bound; `_blob_chunk_size(max_size)` picks the real chunk so >= ~4 chunks fit per part.
- Duplicate-content files: each path's own mtime goes on its `BackupRecord` (`rel_path_to_mtime_ns`), not the group representative's.

## Testing with the generated set
1. `scripts/make-test-media.py` (writes under `$FILESYSTEM_ROOT`, default `./mnt_ro`).
2. Settings > Storage locations: add it as a "Files" location and scan.
3. Settings > Backup archiving: min 1 MiB, max 64 MiB (clump is fixed at 64 MiB).
4. Libraries > Backup to Cloud Archive; watch Copy Jobs > Backup Runs. Run it again: everything should be skipped as unchanged.
5. Restore a few files from the catalog and compare (`cmp`) with the originals.
Tests: `worker/tests/test_backup_tracking.py` (scratch DB only - see HANDOFF.md; needs the repo root mounted for the generator test).
