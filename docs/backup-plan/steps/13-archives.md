# Step 13: Per-library archives

- **Depends on:** 11 (run tracking), 12 (key versions). Replaces the single implicit cloud bucket / local target with explicit archives.

## Model
- **Archive** = a `StorageLocation` with `is_backup_target=True`. `location_type="gcs"` -> `path = gs://bucket[/folder]`; `"local"` -> a folder under the media root. Any number of each.
- **Library** = a local `StorageLocation` with `is_backup_target=False`. `archive_location_id` (one archive) + `archive_subpath` (folder inside it, cloud only).
- Credentials: still one `CloudStorageConfig` service account, shared by every cloud archive.

## Where things are
- `web/app/archive_paths.py` (mirrored in `worker/app/archive_paths.py`): `parse_gcs_path`, `normalize_prefix` (rejects `..`), `format_gcs_path`. Tests in both `tests/test_archive_paths.py`.
- Worker `backup_run._resolve_target(destination, source, cloud_config)` -> (bucket, prefix). The library's subfolder applies only when the library is assigned to that archive. Archives without a `gs://` path fall back to `CloudStorageConfig.bucket_name/prefix`.
- Worker `tasks._restore_v2` builds its backend from the archive's bucket.
- Web helpers: `_archive_for_media_file`, `_backup_files_to_archive` (cloud -> `_start_backup_run`, local -> legacy `_plan_backup`), `_has_backup` (v2 `BackupRecord` or legacy CopyJob).
- Settings > Cloud Storage > 3. Bucket Resolution > "Add as archive" (bucket + directory browser via `GET /settings/cloud-storage/browse`) posts to `POST /settings/cloud-storage/archives` (checks bucket access); the list of all archives is the separate Settings > Archives card. Libraries: `POST /libraries/archives` (local), `POST /archives/{id}/delete` (refused while libraries use it or it holds recorded backups), `GET /libraries/browse-archive`, `POST /libraries/{id}/backup`.
- Startup migration `_migrate_library_archives`: `gcs://` -> `gs://` (idempotent); default assignment of libraries only the first time the column is added.

## Traps
- Skip-unchanged is keyed by (file, archive) - a new subfolder does not re-upload already-backed-up files.
- `browse-archive` lists only cloud archives; local subfolders are not supported.
- Verify still doesn't understand v2 backups; the route says so instead of failing silently.
- Tests must never touch the dev DB (see HANDOFF.md).

## Verified end to end (2026-09-19, test bucket)
New archive `gs://gen_bu_lrg/e2e-test` created through the route; library assigned with subfolder `set-a`; backup via `/libraries/{id}/backup` produced 12 archives + 12 indexes, all under `e2e-test/set-a/`; catalog showed them backed up; Restore route completed and the file was byte-identical. Test objects and rows were then removed.
