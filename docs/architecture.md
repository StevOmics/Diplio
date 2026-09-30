# Architecture

MediaBridge is a small set of Docker Compose services sharing one Postgres database. There's no message format or API contract between services beyond "read/write the same tables" — `web` and `worker` each define their own copy of the SQLAlchemy models (see [`data-model.md`](data-model.md)) against the same schema.

## Services

| Service | Image built from | Purpose |
|---|---|---|
| `db` | `postgres:16-alpine` | Catalog + all app config |
| `web` | `./web` | FastAPI + Jinja UI: auth, catalog browsing, settings, libraries, backup/restore/verify status |
| `rabbitmq` | `rabbitmq:3-management` | Celery broker |
| `worker` | `./worker` | Celery worker on the single `copy` queue — backup runs, restore runs and verifies |
| `flower` | `./flower` | Celery monitoring UI |
| `terminator` | `./terminator` | Internal-only API that can restart the `worker` container via the Docker socket |

Everything long-running is one Celery task type on one queue: `run_backup`, `restore_run`, `verify_v2_batch`. There is no default-queue worker and no beat schedule (the per-file legacy pipeline that needed them was removed; see `backup-plan/steps/17-remove-legacy.md`).

### terminator

`terminator` is the only service with access to `/var/run/docker.sock`. It exposes a minimal internal REST API (`POST /services/{service}/restart`) that only `web` calls, gated by a shared `TERMINATOR_API_KEY` header and an allowlist of restartable services (`worker`). No host port is published — it's reachable only from other containers on the Compose network. This exists so the Settings page can offer a "restart worker" button without giving `web` itself (a user-facing service) direct access to the Docker socket.

## Request/data flow

1. **Scan**: `web` walks each configured library's filesystem path, classifies each file by extension into a `media_type` (see [`media-types.md`](media-types.md)), computes a content fingerprint, and upserts a `MediaFile` row. NFO sidecar parsing (title/year/IMDb/TMDb/rating) runs for movie files.
2. **Backup**: `web` creates a `BackupRun` row and enqueues `run_backup`. `worker` skips files unchanged since their last backup (size + mtime, without hashing), hashes the rest (SHA-256), records files whose content is already stored at the archive instead of re-uploading them, then optionally gzips, optionally encrypts, packs into tar archives, uploads each (confirming size and CRC32C), writes its index file, and records it. Progress (phase, bytes, heartbeat) is written to the `BackupRun` row as it goes. See `cfa-spec.md` and `backup-plan/` for the format.
3. **Restore**: `web` creates a `RestoreRun` and enqueues `restore_run`. `worker` rebuilds each file from its ledger (concatenate parts, decrypt, gunzip), verifies the SHA-256, and only then moves it into place — so a bad archive never overwrites a good file. One file failing doesn't stop the rest.
4. **Verify**: `web` enqueues `verify_v2_batch` (batched per archive). Shallow verify checks each archive's object size + CRC32C and its index file once per batch and samples the ends of the data; deep verify re-downloads and re-hashes. Results land on the file's `BackupRecord`.

### Archives: clump, single, part

Every file lands in exactly one kind of tar archive (`worker/app/packer.py`): files under the min size are **clumped** together (many per archive, up to the clump size), files between min and max are **single**, and files over the max are **split** into **parts**. Each archive has a plaintext `<id>.json` sidecar (same folder, same basename as its `.tar`) mapping file identifiers to their offsets, so a single file can be fetched with a ranged read — a plain archive's entries are keyed by the real SHA-256, an encrypted one's by a keyed HMAC instead (see Encryption below). The `BackupRecordArchive` join table lets one `BackupRecord` point at one archive (single), several in sequence (parts), or share one with other records (a clump) — see [`data-model.md`](data-model.md).

### Cloud cost

The bucket may be Archive/Coldline class, where every request and every byte read back is billed. So: a re-sync of unchanged files makes no bucket requests; content already stored at the archive (a rename, move, copy or touch) is recorded rather than re-uploaded; and shallow verify is batched so its cost tracks the number of archives, not files. Details in `backup-plan/steps/16-cheap-sync-and-verify.md`.

### Retry behavior

There is no automatic retry. A failed backup run is simply re-run — skip-unchanged means it resumes cheaply, and archives already uploaded and recorded are kept. A failed restore reports per-file errors and can be started again.

## Encryption

When enabled, each file is encrypted into one AES-256-GCM blob in independently authenticated chunks (`worker/app/encryption.py`) before it is clumped or split. One master key is derived from the backup passphrase (PBKDF2-HMAC-SHA256); each file's key is derived from the master key plus the file's SHA-256, so nothing per-file is stored separately — just the passphrase (or its recorded key version) and the hash already in the catalog. Tar member names in an encrypted archive are `<content_id>.file` — a keyed HMAC of the SHA-256, not the real hash itself — and the index carries sealed paths, so the bucket's listing reveals no file names *or* which specific files it holds (the real SHA-256 stays database-only; a keyed identifier means a candidate file can't be hashed and checked against the bucket without the master key, unlike a plain hash). See `backup-plan/DEVIATIONS.md` D14.

## Compression

Optional and off by default: each file may be gzipped (standard gzip) before it is encrypted. The SHA-256 is always of the original file; what was done is recorded per file (`BackupRecord.compression`). Already-compressed formats and files that would shrink by under 5% are stored as-is. See `backup-plan/steps/14-compression.md`.

## Cloud storage

Google Cloud Storage is the only backup destination. Real uploads are throttled to a fraction of the last measured speed-test bandwidth (`CLOUD_UPLOAD_THROTTLE_FRACTION` in `web/app/main.py`, `UPLOAD_THROTTLE_FRACTION` in `worker/app/gcs.py` — these two constants must be kept in sync by hand) so a backup run doesn't saturate the connection.

## No migration tool

Schema changes are applied via `Base.metadata.create_all` on `web`'s FastAPI startup, which only creates *missing tables* — it does not alter existing ones. Adding a column to an existing table needs an explicit, hand-written, idempotent guard, and removing one needs an explicit `DROP ... IF EXISTS` (see `web/app/main.py:on_startup` for both). There's no Alembic (or similar) in this repo yet. In dev, `web` runs with `--reload`, so editing `main.py` re-runs the startup migration.
