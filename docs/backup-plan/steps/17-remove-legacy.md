# Step 17: Remove the legacy backup pipeline

- **Decision:** v2 is the only backup strategy and this is the only instance (no legacy backups exist), so the old per-file pipeline is deleted rather than kept "frozen". Done in three commits: restore runs first (restore was the last thing riding on the legacy path), then the worker, then the web app and schema.
- **Local-folder archives are gone too** (cloud-only): the v2 run never supported them, and the instance has none. If a second destination is ever wanted, give the v2 pipeline a local `StorageBackend` (the test double `LocalBackend` is most of it) rather than reviving the old path.

## What was removed
| Area | Removed |
|---|---|
| Worker tasks | `copy_media_file`, `backup_clump`, `verify_copy_job`, `retry_failed_transfers`, `ping`, and their helpers (`_ledger_parts`, `_record_backup`, `_materialize_archive`, `_backup_split`, `_restore_from_parts`, `_run_plain_copy`, ...) — `tasks.py` 1712 -> ~700 lines |
| Worker other | chunk-manifest `encrypt_file`/`decrypt_file`/`backup_exists`/`derive_path_id` (only the v2 blob format remains); `gcs.delete_blobs_with_prefix` |
| Services | `celery` (default queue) and `celery-beat` (retry sweep); terminator allowlist is `worker` only; the `/data/backup` mounts |
| Web routes | per-file and bulk local backup / restore / verify / copy, `_plan_backup` / `_execute_backup_plan`, local-archive creation, set/unset local backup target, `/settings/transfer`, `/settings/transfer-retry`, `/settings/internet-speed`, copy-job start/cancel/pause |
| Web UI | the legacy job tables and "pending backups" confirmations on the Copy Jobs page (now **Backups & Restores**), the Local Storage card, the Transfer / Network Throttling / Internet Speed settings sections |
| Schema | table `copy_jobs`; `transfer_config` columns `max_speed_mbps`, `max_size_gb`, `split_over_percent`, `min_size_mb`, `clump_under_percent`, `clump_split_enabled`, `retry_count`, `retry_interval_minutes`, `internet_speed_mbps`, `measure_speed_on_transfer`; `backup_archives` columns `checksum`, `encryption_key_id`, `is_clump` |

Schema drops are `DROP ... IF EXISTS` in `web/app/main.py:on_startup` (idempotent). Kept: `backup_records.local_checksum` (still written by the run and read by the catalog's staleness display).

## What replaced them
- **Restore**: `RestoreRun` + `worker/app/restore_run.py` (`restore_run`), with live progress and a per-file result list; `_restore_v2` no longer needs a job row (`on_progress` callback, returns bytes).
- **Progress**: `_Progress` takes a pluggable updater, shared by backup and restore runs; the status endpoint and page JS handle both (`backup-<id>` / `restore-<id>` keys).
- **Routes kept**: `/libraries/{id}/backup`, `/movies/{id}/backup-cloud`, `/movies/bulk-backup-cloud`, `restore-cloud` / `bulk-restore-cloud`, `verify-cloud` / `bulk-verify-cloud`, all cloud/archive/key/compression/encryption settings.

## Notes
- The web container runs with `--reload` in dev, so editing `main.py` re-runs the startup migration: the drops hit the live DB mid-refactor. Nothing valuable was in `copy_jobs` (one old test row) but take a dump first if that ever matters.
- `.claude/agents/plan.md`, `review.md`, `implement.md` still say "legacy backups must stay restorable" (written for the v2 migration); that no longer applies here. Left for the owner to reword.
- `web/app/templates/admin.html` and `settings copy.html` (untracked, not part of this work) still reference the removed celery restart route.
