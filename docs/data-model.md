# Data model

All tables live in one Postgres database, defined in `web/app/models.py` (the source of truth — `worker/app/models.py` mirrors the subset `worker` actually touches, by hand; see [`architecture.md`](architecture.md#no-migration-tool)).

## Catalog

**`StorageLocation`** — a folder MediaBridge knows about: either a source library or a backup target.
- `location_type`: `"local"` or `"gcs"`.
- `media_type`: what kind of content this location holds (`"movies"`, `"music"`, `"photos"`, `"documents"`, or `"files"` as a misc catch-all) — drives which file extensions get cataloged on scan. See [`media-types.md`](media-types.md).
- `is_backup_target`: at most one **local** location and, independently, at most one **gcs** location can be `true` at a time. A file can be backed up to both simultaneously.

**`MediaFile`** — one cataloged file.
- Identity: `path` (unique), `uuid` (used to derive encryption keys — stable even if the file moves storage locations), content `fingerprint` (size + partial BLAKE2b, identifies content independent of path).
- `media_type`: set from the owning location's type during scan (or classified per-file, for a `"files"` location — see [`media-types.md`](media-types.md)).
- `genre`: the file's immediate parent folder name relative to its library root — a generic category, not movie-specific despite the name (kept as-is rather than renamed, to avoid a breaking column rename with no migration tool).
- Movie-specific enrichment (nullable, populated from `.nfo` sidecars): `title`, `overview`, `year`, `imdb_id`, `tmdb_id`, `rating`, `runtime_minutes`.
- Jellyfin watch data (nullable, populated by a sync): `jellyfin_item_id`, `jellyfin_library`, `watched`, `play_count`, `last_played_at`.

## Backup pipeline

**`BackupRecord`** — the *current* backup state for one `(media_file, destination)` pair, upserted on every successful backup. One unique row per pair — this only ever reflects the latest state. A file backed up to two archives gets two independent rows. `compression` is `gzip` when the file's bytes were gzipped before encryption/packing, else NULL (as-is) - restore and verify follow this, not the current setting. `sha256` is the file's real content hash — kept database-only, never written into a bucket object (see `BackupRecordContentId` below and `backup-plan/DEVIATIONS.md` D14).

**`BackupRecordContentId`** — one row per `(backup_record, key_version)` pair that record's *content-id* (a keyed HMAC of `sha256`, see D14) has been computed under. Encrypted-archive index entries and tar member names are identified by content-id instead of the real `sha256`, so a candidate file can't be hashed and checked against the bucket's contents without the master key. Rows accumulate across key rotations (backfilled automatically — `web/app/key_versions.py:backfill_content_ids`) so dedup keeps recognizing previously-archived content after the key changes. Plain (unencrypted) archives are unaffected — their index entries stay keyed by the real `sha256` directly.

**`BackupArchive`** — one physical blob that exists on a destination: a plain single-file copy, one part of a split file, or a shared clump body.

**`BackupRecordArchive`** — join table between `BackupRecord` and `BackupArchive` (many-to-many), with `part_index`/`archive_offset`/`archive_length` to locate a record's bytes within an archive. This one table represents all three shapes:
- **Normal backup**: one row, spanning the whole archive.
- **Split** (file too large): several rows, one per archive, `part_index` 0..N-1.
- **Clump** (several small files sharing one archive): one row per member file, each with its own `archive_offset`/`archive_length` slice of the same shared archive.

```
MediaFile ──┐
            │
            └─< BackupRecord (current state, per destination) ──< BackupRecordArchive >── BackupArchive
```

## Singleton config tables

Each of these has at most one row, read/written from the Settings page:

- **`BackupEncryptionConfig`** — whether encryption is on, and the passphrase (see below on plaintext storage).
- **`TransferConfig`** — the packing sizes (`min_size_bytes`, `clump_size_bytes`, `max_size_bytes`) and optional compression (`compression_enabled`, `compression_level`).
- **`CloudStorageConfig`** — GCS service account key, selected bucket, measured upload/download speed.
- **`JellyfinConfig`** — server URL, API key, which Jellyfin user's watch data to sync, and the path-prefix mapping between Jellyfin's and MediaBridge's view of the filesystem (both containers mount the same folder, typically at different paths).

### Why secrets are stored in plaintext

`BackupEncryptionConfig.password`, `CloudStorageConfig.service_account_json`, and `JellyfinConfig.api_key` are all stored as plaintext columns, deliberately — this lets scheduled/background transfers run unattended without a running keyring or an unlock step. The tradeoff: anyone with read access to the database can read these secrets. This is a reasonable tradeoff for a trusted single-host deployment; it's worth revisiting before running MediaBridge anywhere with a wider database-access surface.
