# Step 18 — Portability: adopting an existing archive's content into a fresh instance

Not part of the original nine-step plan (`docs/backup-plan/PLAN.md`) or `docs/cfa-spec.md`'s scope
(§8 explicitly excludes anything cross-instance) - this documents a feature that shipped ahead of
its own docs, plus the increment that closes its two remaining gaps. See `DEVIATIONS.md` D13.

## Scenario

Computer A has a library backed up to a cloud archive. Computer B is a fresh (or reset) install -
its database has never heard of that content. The user re-enters the same master key and points a
library at the same archive+subpath. Portability lets B "reconnect" that content: rebuild its own
`MediaFile`/`BackupRecord`/`BackupArchive`/`BackupRecordArchive` rows straight from the bucket's own
index files, so a future backup of that library finds everything already stored
(`_adopt_existing_copies`, D11) and re-uploads nothing.

## What already existed

- `SyncRun` (`web/app/models.py` / `worker/app/models.py`) - tracked like `BackupRun`/`RestoreRun`
  (queued -> running -> done/partial/failed, live progress), shown on Backups & Restores.
- `worker/app/bucket_inventory.py` (`discover()`) - lists every index file under a prefix and groups
  entries by content hash, with no database row to start from. Pure over its arguments (a storage
  backend, prefix, and a path-resolving callback) - testable with `tests/storage_double.LocalBackend`,
  no GCS/DB/crypto imports.
- `worker/app/sync_run.py` (`_execute_sync_run`, `_all_candidate_keys`, `_ContentResolver` - renamed
  from `_PathResolver` by D14, see below) - owns the database and encryption-key side: tries the
  current master key plus every retired `BackupKeyVersion` per archive, caching which key (if any)
  opens a given `archive_id` so a clump of many files costs at most one decrypt attempt per
  candidate key, not one per file. Adopts matches into the four tables above via `_restore_v2` (same
  code restore already uses).

  **2026-09-26 (D14):** a content-id-keyed index's dict key is a keyed HMAC, not the real sha256 -
  `bucket_inventory.discover` gained a second callback, `resolve_sha256`, that decrypts each entry's
  sealed `"sha256_enc"` field to recover the real hash *before* `resolve_paths` is called with it
  (paths_enc's key and AAD are the real sha256, not content_id). `_PathResolver` was renamed
  `_ContentResolver` and gained `resolve_sha256`, sharing its per-archive key cache with
  `resolve_paths`. `web/app/encryption_check.py:check_key_match` (the Libraries-page proactive
  check below) needed the identical fix. See `DEVIATIONS.md` D14.
- Web routes `POST /libraries/{id}/discover-bucket` (read-only bucket listing, metadata only - no
  index files read, no bytes moved) and `POST /libraries/{id}/sync-from-bucket` (starts the
  `SyncRun`), with a manual "Check bucket" -> "Sync N from bucket" button pair on the Libraries page.

## Gaps this step closes

1. **`discover-bucket` never tried to decrypt anything.** It only compared bucket object keys
   against the database (`web/app/cloud_inventory.py:build_report`), so it couldn't tell the user
   whether their currently-configured key actually opens what it found - that only happened inside
   the Celery `sync_run` task, after the user had already committed to syncing.

   Fixed with a new `web/app/encryption_check.py`: pure crypto helpers mirroring
   `worker/app/encryption.py` (`derive_master_key`, `derive_file_key`, `decrypt_paths`) plus
   `all_candidate_keys` (same logic as `worker/app/sync_run.py:_all_candidate_keys`, against
   `web/app/models.py`) and `check_key_match(index_keys, read_object, candidate_keys)`, which reads
   each index.json (cheap - small plaintext/base64 JSON, no tar payload reads, same cost profile
   `bucket_inventory.discover` already relies on) and reports one of:
   - `match` - every encrypted entry seen decrypted with a candidate key
   - `no_match` - encrypted entries were found but none decrypted
   - `mixed` - some archives decrypted, others didn't
   - `unencrypted` - index files were found but none were encrypted
   - `no_content` - no well-formed index files were found at all

   `discover_bucket_contents` (`web/app/main.py`) now calls this whenever `report.untracked` is
   non-empty and adds `key_status`/`key_message` to its response. `web/app/gcs.py` gained
   `read_object` (a single small-object download) to support it - `list_objects` alone (metadata
   only) isn't enough to read index file contents.

2. **No proactive UI.** The user had to know to click "Check bucket"; a decrypt failure was a
   silent per-file `"skipped"` entry in `SyncRun.results_json`, never surfaced up front.

   Fixed two ways:
   - `update_storage_location` (`web/app/main.py`) now runs the same discover-bucket check
     automatically, once, right after a library is newly pointed at a cloud archive+subpath - the
     result is cached on a new `StorageLocation.discover_report_json` column (same JSON shape the
     manual endpoint returns) so the Libraries page can render it on load without a button click.
     Best-effort: a failed auto-check just leaves the cache empty, same as before this existed.
   - The Libraries page JS (`mbDiscoverBucket`/new `mbRenderDiscoverResult` in
     `web/app/templates/libraries.html`) now renders an A/B/C prompt when content is found:
     - **(A) Sync** - the existing "Sync N from bucket" button, unchanged.
     - **(B) Pick a different folder** - opens the existing edit-row path field (`mbEditRow`) so
       the user can repoint the library's local path before syncing; no new backend route (per-file
       path remapping was explicitly declined as unnecessary - see TODO.md).
     - **(C) Ignore for now** - dismisses the box; client-side only, reappears next page load (not
       "permanently silence" - see TODO.md for that as a possible follow-up).

     A `key_status` of `no_match`/`mixed` renders a warning block (reusing the existing
     `no_index_count` warning styling) pointing at Settings > Backup Encryption, with its own
     "Ignore for now" dismiss.

## Deliberately not built

- Per-file path remapping for option B - repointing the whole library's local path already covers
  the only scenario in view (a fresh install choosing where files should live).
- A persistent "permanently ignore this library's mismatch" state - today's dismiss is
  session-local (reappears on reload).
- Everything already deferred under D7 (index-file fallback when the *database* is down, as
  opposed to "never had a row for this content") stays deferred; this step repurposes the same
  `bucket_inventory.discover` mechanism for a different trigger, not the D7 scenario itself.
