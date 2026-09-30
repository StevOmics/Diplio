import uuid as uuid_lib
from datetime import datetime, time
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, Time, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class StorageLocation(Base):
    __tablename__ = "storage_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    # "local" is the only supported type today; the column exists so remote
    # storage backends can be added later without a schema change.
    location_type: Mapped[str] = mapped_column(String, default="local")
    # What kind of content this location holds - drives which file extensions
    # get cataloged on scan (app.config.MEDIA_TYPE_EXTENSIONS / MEDIA_TYPES).
    # "files" catalogs anything, tagging each file with its real type when
    # recognized and "files" itself otherwise (misc catch-all) rather than
    # the location's declared type. This is what makes the catalog generic
    # across movies/music/photos/documents instead of movies-only.
    media_type: Mapped[str] = mapped_column(String, default="movies")
    path: Mapped[str] = mapped_column(String, unique=True)
    # At most one location is the backup target at a time; Catalog uses it to
    # show per-file backup status and offer a one-click "Back up" action.
    is_backup_target: Mapped[bool] = mapped_column(Boolean, default=False)
    # v2 backup pipeline (docs/cfa-spec.md section 2 "sources") - newline-separated
    # glob patterns (see app.backup_settings.parse_exclude_globs) to skip when
    # backing up this source, null meaning no exclusions. Nothing reads this
    # yet - see docs/backup-plan/steps/01-settings.md.
    exclude_globs: Mapped[Optional[str]] = mapped_column(Text)
    # The archive (a backup-target StorageLocation, local or gcs) this library
    # backs up to, and an optional folder inside it. One archive per library.
    archive_location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="SET NULL"), nullable=True
    )
    archive_subpath: Mapped[Optional[str]] = mapped_column(String)
    # GCS storage class new objects are written with (app.gcs.STORAGE_CLASSES).
    # Chosen once when a cloud archive is added and never edited; NULL means
    # "use the bucket's default class".
    storage_class: Mapped[Optional[str]] = mapped_column(String)
    # A library the user has deliberately stopped tracking (setup.sh --fs-root's
    # "Stop tracking" option, or the equivalent Libraries page action) - excluded
    # from catalog.scan_library so it's never rescanned, but its StorageLocation,
    # MediaFile and BackupRecord rows are all left alone (not deleted), so its
    # cloud backups stay visible and restorable from the Catalog page at any
    # time. Distinct from deleting the library outright.
    untracked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Per-library encryption override. NULL = inherit BackupEncryptionConfig's
    # global on/off; True/False forces this library regardless of the global
    # setting. A run still needs a master key configured to actually encrypt -
    # this only decides whether a library WANTS encryption, not the key
    # itself, which stays global (one passphrase for the whole app). Mixed
    # encrypted/unencrypted content coexists fine at one archive: BackupArchive
    # already records `encrypted` per archive object, independent of others
    # at the same destination.
    encrypted: Mapped[Optional[bool]] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Cached result of the portability discover-bucket check (see
    # web/app/encryption_check.py, docs/backup-plan/steps/18-portability.md),
    # same JSON shape POST /libraries/{id}/discover-bucket returns. Populated
    # automatically the first time a library is pointed at a cloud archive
    # (update_storage_location) and refreshed on every manual "Check bucket"
    # click, so the Libraries page can show the A/B/C prompt on load instead
    # of requiring the button first.
    discover_report_json: Mapped[Optional[str]] = mapped_column(Text)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String, unique=True, index=True, default=lambda: str(uuid_lib.uuid4()))
    path: Mapped[str] = mapped_column(String, unique=True, index=True)
    filename: Mapped[str] = mapped_column(String, index=True)
    extension: Mapped[str] = mapped_column(String)
    genre: Mapped[Optional[str]] = mapped_column(String, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    # Written on every scan (catalog.py:_scan_location) - a size or mtime
    # change since the last scan means the file's content changed locally
    # (reported in the scan result), and triggers a fingerprint recompute.
    # NULL only until the first scan after this column existed.
    mtime_ns: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Set from the owning StorageLocation's media_type during a scan (or,
    # for a "files" location, classified per-file by extension - see
    # catalog.py:_scan_location).
    media_type: Mapped[str] = mapped_column(String, default="movies")
    storage_location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # Set by a scan when the file is no longer on disk. The row is kept so the
    # catalog still lists it (and its backups stay restorable); cleared if it reappears.
    is_missing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Content fingerprint (size + first/last ~1MiB, BLAKE2b) - identifies a file's
    # content independent of its path, without hashing multi-GB files in full.
    fingerprint: Mapped[Optional[str]] = mapped_column(String, index=True)

    # Enrichment parsed from a sibling .nfo sidecar file, when present.
    title: Mapped[Optional[str]] = mapped_column(String)
    overview: Mapped[Optional[str]] = mapped_column(Text)
    year: Mapped[Optional[int]] = mapped_column(Integer)
    imdb_id: Mapped[Optional[str]] = mapped_column(String)
    tmdb_id: Mapped[Optional[str]] = mapped_column(String)
    rating: Mapped[Optional[float]] = mapped_column(Float)
    runtime_minutes: Mapped[Optional[int]] = mapped_column(Integer)

    # Watch data synced from Jellyfin, for the one user configured in JellyfinConfig.
    jellyfin_item_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    jellyfin_library: Mapped[Optional[str]] = mapped_column(String, index=True)
    watched: Mapped[bool] = mapped_column(Boolean, default=False)
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class BackupArchive(Base):
    """A single physical file that exists on a backup destination. Usually holds
    exactly one MediaFile's data (see BackupRecordArchive below), but a clump
    holds several files' data packed together, and one very large file's data
    can span several archives (a split)."""

    __tablename__ = "backup_archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # v2 backup pipeline (docs/cfa-spec.md sections 4/6.4) - all nullable, and
    # NULL on all four together means a pre-v2 archive, which the legacy
    # restore path handles. archive_id is the packer's uuid4; the unique index
    # on it (see web/app/main.py on_startup) is fine with many NULLs since
    # Postgres doesn't enforce uniqueness among them. indexed_at is set only
    # after the index object is written (section 6 step 5) - indexed_at IS
    # NULL is how step 7's restore identifies an incomplete archive ("An
    # archive with no index file is incomplete and is ignored by restore").
    # unique=True as well as index=True: on a fresh install create_all builds
    # this index, and without unique=True it would build a non-unique one
    # under the same conventional name (ix_backup_archives_archive_id), after
    # which on_startup's CREATE UNIQUE INDEX IF NOT EXISTS would silently
    # no-op and leave archive_id unenforced. Postgres permits many NULLs in a
    # unique index, so every pre-v2 row still coexists.
    archive_id: Mapped[Optional[str]] = mapped_column(String(36), unique=True, index=True)
    archive_type: Mapped[Optional[str]] = mapped_column(String)  # "clump" / "single" / "part"
    crc32c: Mapped[Optional[str]] = mapped_column(String)  # base64, as confirmed at upload
    indexed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Which BackupKeyVersion encrypted this v2 archive; NULL for plain and legacy
    # archives (restore then falls back to the current key).
    key_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("backup_key_versions.id", ondelete="SET NULL"), nullable=True)


class BackupRecord(Base):
    """The current backup state of one (MediaFile, destination) pair - one row
    per file per backup destination, updated in place on every successful
    backup to that destination. Unlike CopyJob (an append-only log of every
    individual backup/restore/verify operation), this is always just "what's
    the latest backup situation for this file at this destination right now".
    A file backed up to both a local target and the cloud archive gets two
    independent rows, so restoring/verifying one destination is unaffected by
    the other."""

    __tablename__ = "backup_records"
    __table_args__ = (
        UniqueConstraint("media_file_id", "destination_storage_location_id", name="uq_backup_record_media_file_destination"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"), index=True)
    destination_storage_location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), index=True
    )
    # Snapshots of the source file as of this backup, kept even if media_files
    # later changes - local_checksum can be compared against the live
    # MediaFile.fingerprint to tell whether the source has drifted since.
    local_path: Mapped[str] = mapped_column(String)
    local_checksum: Mapped[str] = mapped_column(String)
    # v2 backup pipeline (docs/cfa-spec.md sections 1.2/6.2/7) - the file's
    # streaming SHA-256 and the mtime_ns it was hashed at, written by step 5
    # and read by steps 6 (skip-check) and 7 (restore-time verification).
    # Nullable because every row from before the v2 pipeline predates this.
    # This is a different thing from local_checksum above (a partial
    # BLAKE2b fingerprint used for drift detection) and does not replace it.
    # Indexed because section 7 restores by hash lookup.
    sha256: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    mtime_ns: Mapped[Optional[int]] = mapped_column(BigInteger)
    # How this file's bytes were stored before encryption/packing: "gzip", or
    # NULL for as-is (every backup made before compression existed).
    compression: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="done")
    verify_status: Mapped[Optional[str]] = mapped_column(String)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BackupRecordArchive(Base):
    """Join table between BackupRecord and BackupArchive. Many-to-many so both
    directions are representable: one record spanning several archives (split)
    and one archive holding several records (clump). archive_offset/
    archive_length locate this record's bytes within the archive - for today's
    ordinary one-record-one-archive backups that's just the whole file."""

    __tablename__ = "backup_record_archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    backup_record_id: Mapped[int] = mapped_column(ForeignKey("backup_records.id", ondelete="CASCADE"), index=True)
    backup_archive_id: Mapped[int] = mapped_column(ForeignKey("backup_archives.id", ondelete="CASCADE"), index=True)
    part_index: Mapped[int] = mapped_column(Integer, default=0)
    archive_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    archive_length: Mapped[int] = mapped_column(BigInteger)


class BackupRecordContentId(Base):
    """One row per (record, key-version) pair the record's HMAC content-id
    (worker/app/encryption.derive_content_id) has been computed under.
    content_id replaces raw sha256 as the identifier exposed in GCS-visible
    artifacts (index JSON keys, encrypted tar member names) so a candidate
    file can't be hashed and checked against our archives without the master
    key. Rows accumulate across key rotations (backfilled cheaply from the
    already-known sha256, no file re-read) so dedup keeps recognizing old
    content after the key changes - see docs/backup-plan/DEVIATIONS.md."""

    __tablename__ = "backup_record_content_ids"
    __table_args__ = (
        UniqueConstraint("backup_record_id", "key_version_id", name="uq_content_id_record_key_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    backup_record_id: Mapped[int] = mapped_column(ForeignKey("backup_records.id", ondelete="CASCADE"), index=True)
    key_version_id: Mapped[int] = mapped_column(ForeignKey("backup_key_versions.id", ondelete="CASCADE"), index=True)
    content_id: Mapped[str] = mapped_column(String(64), index=True)


class BackupEncryptionConfig(Base):
    __tablename__ = "backup_encryption_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Plaintext, like JellyfinConfig.api_key - stored so backups can run
    # unattended. This protects backups from anyone with access only to the
    # backup destination (e.g. untrusted remote/cloud storage), not from
    # anyone with access to this database.
    password: Mapped[Optional[str]] = mapped_column(String)
    # Salt for deriving the master key from the password (PBKDF2). Not secret,
    # just needs to be fixed once a password is set.
    kdf_salt: Mapped[Optional[str]] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TransferConfig(Base):
    __tablename__ = "transfer_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    # v2 backup pipeline packing sizes (docs/cfa-spec.md sections 2 and 3), in bytes:
    # splitting computes k = ceil(size / (max_size - 1 MiB)), and float rounding
    # there would change part counts.
    min_size_bytes: Mapped[int] = mapped_column(BigInteger, default=10485760)  # 10 MiB
    clump_size_bytes: Mapped[int] = mapped_column(BigInteger, default=67108864)  # 64 MiB
    max_size_bytes: Mapped[int] = mapped_column(BigInteger, default=1073741824)  # 1 GiB
    # Caps how much data a single BackupRun asks the worker to hold on local
    # disk at once (worker/app/backup_run.py's _prepare_candidates writes
    # every file's compressed+encrypted copy before packing starts, so a
    # whole-library run's disk peak scales with the run's total size). A
    # library/selection backup larger than this gets split into several
    # BackupRuns instead of one - see main.py's _start_batched_backup_runs.
    # Doesn't bound a single very large file, only how many files land in one run.
    batch_bytes: Mapped[int] = mapped_column(BigInteger, default=5368709120)  # 5 GiB
    # Optional gzip of each file before encryption. Files that don't shrink
    # enough are stored as-is regardless.
    compression_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    compression_level: Mapped[int] = mapped_column(Integer, default=6)
    # Hard ceiling on upload speed, in Mbps - NULL means "no explicit limit",
    # in which case worker/app/gcs.py:effective_upload_mbps falls back to
    # half of CloudStorageConfig.upload_mbps (the last speed test), or
    # DEFAULT_UPLOAD_CAP_MBPS (15) before any speed test has run. Set this to
    # cap it lower (e.g. 5) regardless of what the connection measures at, or
    # higher to raise the automatic default's ceiling.
    max_upload_mbps: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CloudStorageConfig(Base):
    """Connection state for an optional cloud storage backend, set up through the
    Settings page's Cloud Storage workflow (upload service account key -> list
    buckets -> select one). Only Google Cloud Storage today; provider exists so
    other backends can be added later without a schema change. This is
    connection setup only - nothing transfers here yet, the same way
    TransferConfig's split/clump settings existed before the pipeline used them."""

    __tablename__ = "cloud_storage_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String, default="gcs")
    # Full service account JSON key, as downloaded from GCP - stored plaintext,
    # like JellyfinConfig.api_key and BackupEncryptionConfig.password, so
    # scheduled transfers can run unattended later.
    service_account_json: Mapped[Optional[str]] = mapped_column(Text)
    service_account_email: Mapped[Optional[str]] = mapped_column(String)
    project_id: Mapped[Optional[str]] = mapped_column(String)
    # JSON-encoded list of bucket names from the last successful "List buckets" -
    # rendered as the picker in Settings. Null until that's been run once.
    available_buckets: Mapped[Optional[str]] = mapped_column(Text)
    bucket_name: Mapped[Optional[str]] = mapped_column(String)
    # v2 backup pipeline (docs/cfa-spec.md section 4) object-key prefix under the
    # bucket, e.g. "server01/" - null means bucket root. Nothing reads this
    # yet - see docs/backup-plan/steps/01-settings.md.
    prefix: Mapped[Optional[str]] = mapped_column(String)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cached result of the last "Refresh" on System Status > Backup Targets
    # (JSON, same shape as POST /system/cloud-inventory's response minus
    # checked_at) so the page still shows it after a reload instead of
    # reverting to "Not checked yet." - the listing itself is never run
    # automatically, only cached here when a user explicitly triggers it.
    last_inventory_json: Mapped[Optional[str]] = mapped_column(Text)
    last_inventory_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Mutually exclusive with StorageLocation.is_backup_target - at most one
    # backup target (local or cloud) is active at a time.
    is_backup_target: Mapped[bool] = mapped_column(Boolean, default=False)
    # Results of the last "Test connectivity & speed" run (gcs.test_connectivity_and_speed) - a real
    # upload/download of a throwaway blob, timed. Used to estimate backup duration.
    last_speed_test_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    upload_mbps: Mapped[Optional[float]] = mapped_column(Float)
    download_mbps: Mapped[Optional[float]] = mapped_column(Float)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class S3StorageConfig(Base):
    """Connection state for AWS S3, set up through Settings > Cloud Storage's
    Amazon Web Services panel (enter an access key id/secret -> list buckets
    -> select one). Mirrors CloudStorageConfig's shape (same field names
    where the concepts line up) but is its own table rather than overloading
    CloudStorageConfig, since S3's access-key-pair auth doesn't fit that
    table's GCS-shaped credential fields (service_account_json/email)."""

    __tablename__ = "s3_storage_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored plaintext, same tradeoff as every other secret in this app (see
    # CLAUDE.md) - unattended scheduled transfers need it available at rest.
    access_key_id: Mapped[Optional[str]] = mapped_column(String)
    secret_access_key: Mapped[Optional[str]] = mapped_column(String)
    region: Mapped[Optional[str]] = mapped_column(String)
    available_buckets: Mapped[Optional[str]] = mapped_column(Text)
    bucket_name: Mapped[Optional[str]] = mapped_column(String)
    prefix: Mapped[Optional[str]] = mapped_column(String)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    last_speed_test_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    upload_mbps: Mapped[Optional[float]] = mapped_column(Float)
    download_mbps: Mapped[Optional[float]] = mapped_column(Float)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class JellyfinConfig(Base):
    __tablename__ = "jellyfin_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_url: Mapped[str] = mapped_column(String)
    api_key: Mapped[str] = mapped_column(String)
    # Jellyfin's watch data is per-user; pick one Jellyfin account to sync against.
    sync_user_id: Mapped[Optional[str]] = mapped_column(String)
    sync_user_name: Mapped[Optional[str]] = mapped_column(String)
    # Same host folder, mounted at different paths in each container - swap one prefix for the other to match files.
    path_prefix_from: Mapped[str] = mapped_column(String, default="/media")
    path_prefix_to: Mapped[str] = mapped_column(String, default="/mnt")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TlsConfig(Base):
    """Singleton tracking the active TLS cert state for the nginx reverse
    proxy. Cert/key bytes live on disk (./certs, see web/app/tls.py) - nginx
    can only read files, so unlike other secrets in this table there's no
    reason to also duplicate them into Postgres. Web-only, not mirrored to
    worker/app/models.py (same precedent as JellyfinConfig)."""

    __tablename__ = "tls_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    custom_domain: Mapped[Optional[str]] = mapped_column(String)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False)
    cert_uploaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Raw textarea contents: one hostname/IP per line, parsed by
    # web/app/tls.py into nginx server_name entries (always listen 80/443
    # internally - any other host-side port is a docker-compose mapping or
    # external proxy concern, not something this app configures). Empty
    # means the generated config falls back to server_name _.
    domains: Mapped[Optional[str]] = mapped_column(Text)
    redirect_http: Mapped[bool] = mapped_column(Boolean, default=True)
    # The address a user actually reaches this instance at (e.g.
    # https://192.0.2.10:9443, or a public domain fronted by an external
    # proxy) - purely informational/display, since nginx's server_name _
    # already accepts any hostname regardless of what's set here.
    external_url: Mapped[Optional[str]] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BackupRun(Base):
    """One v2 backup run (worker/app/backup_run.py), tracked so the UI can show
    library / single-file / selected-files backups as they happen. Created by
    the web app as "queued"; the worker moves it through running ->
    done | partial | failed. Distinct from CopyJob (the legacy per-file job
    log) - v2 runs are archive-based, not per-file jobs."""

    __tablename__ = "backup_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "library" (whole library), "file" (one file), "selection" (checked files).
    scope: Mapped[str] = mapped_column(String, default="library")
    source_storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"))
    destination_storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"))
    # JSON list of MediaFile ids for "file"/"selection"; NULL means the whole library.
    file_ids: Mapped[Optional[str]] = mapped_column(Text)
    # queued | running | done | partial | failed
    status: Mapped[str] = mapped_column(String, default="queued")
    # "replace_older" (skip files unchanged since last backup, today's default)
    # or "replace_all" (force re-upload everything in scope).
    mode: Mapped[str] = mapped_column(String(20), default="replace_older", server_default="replace_older")
    files_total: Mapped[int] = mapped_column(Integer, default=0)
    files_skipped: Mapped[int] = mapped_column(Integer, default=0)
    archives_total: Mapped[int] = mapped_column(Integer, default=0)
    archives_done: Mapped[int] = mapped_column(Integer, default=0)
    bytes_uploaded: Mapped[int] = mapped_column(BigInteger, default=0)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Human-readable phase while running ("Encrypting", "Uploading 3/9"...).
    detail: Mapped[Optional[str]] = mapped_column(String)
    # Live progress of the current phase ("hashing" | "preparing" | "packing" |
    # "uploading"), written by the worker at most about once a second. done /
    # total are files for hashing and preparing, bytes for uploading; total 0
    # means "no known total" (packing). heartbeat_at moves on every write, so
    # a run that says "running" but hasn't beat for minutes is stuck or dead.
    phase: Mapped[Optional[str]] = mapped_column(String)
    phase_done: Mapped[int] = mapped_column(BigInteger, default=0)
    phase_total: Mapped[int] = mapped_column(BigInteger, default=0)
    phase_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    # JSON list of {"path", "reason"} for skipped files.
    skipped_json: Mapped[Optional[str]] = mapped_column(Text)
    # JSON list of {"path", "size_bytes"} for files actually packed/uploaded
    # this run - distinct from skipped (excluded/unchanged/already-backed-up)
    # and from adopted-existing-copies, which land in skipped too.
    backed_up_json: Mapped[Optional[str]] = mapped_column(Text)
    celery_task_id: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class RestoreRun(Base):
    """One restore of one or more files from an archive back to their original
    paths (worker/app/restore_run.py), tracked so the UI can show progress.
    Created by the web app as "queued"; the worker moves it through
    running -> done | partial | failed (partial = some files restored, some
    not). Mirrors BackupRun's live-progress columns."""

    __tablename__ = "restore_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    destination_storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"))
    # "file" (one file) or "selection" (checked files).
    scope: Mapped[str] = mapped_column(String, default="selection")
    # JSON list of MediaFile ids to restore.
    file_ids: Mapped[str] = mapped_column(Text)
    # queued | running | done | partial | failed
    status: Mapped[str] = mapped_column(String, default="queued")
    # "replace_older" (won't overwrite a local file already as new as the
    # restored version, today's default) or "replace_all" (unconditional overwrite).
    mode: Mapped[str] = mapped_column(String(20), default="replace_older", server_default="replace_older")
    files_total: Mapped[int] = mapped_column(Integer, default=0)
    files_done: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)
    bytes_restored: Mapped[int] = mapped_column(BigInteger, default=0)
    detail: Mapped[Optional[str]] = mapped_column(String)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    # JSON list of {"path", "status": "restored"|"failed"|"skipped", "error"?/"reason"?} per file.
    results_json: Mapped[Optional[str]] = mapped_column(Text)
    # Live progress, same meaning as on BackupRun (phase is "restoring"; done /
    # total are bytes).
    phase: Mapped[Optional[str]] = mapped_column(String)
    phase_done: Mapped[int] = mapped_column(BigInteger, default=0)
    phase_total: Mapped[int] = mapped_column(BigInteger, default=0)
    phase_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    celery_task_id: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class SyncRun(Base):
    """Portability (docs/backup-plan/steps/18-portability.md): pulling a
    library's already-existing backup content down from a bucket that a
    *different* (or reset) instance wrote to - discovered via
    POST /libraries/{id}/discover-bucket, started via .../sync-from-bucket.
    worker/app/sync_run.py reads the archive's index files directly (this
    instance's database has no rows for this content yet, by definition),
    reconstructs each file locally, and - unlike a normal restore - also
    creates the MediaFile/BackupRecord/BackupArchive/BackupRecordArchive rows
    a normal backup would have left, so this library is fully "reconnected":
    a future backup of it finds everything already stored and uploads
    nothing. Mirrors RestoreRun's shape/live-progress columns."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The local library being populated.
    library_storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"))
    # The archive (bucket) being read from - same FK target as BackupRun's
    # destination_storage_location_id.
    archive_storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"))
    # queued | running | done | partial | failed
    status: Mapped[str] = mapped_column(String, default="queued")
    files_total: Mapped[int] = mapped_column(Integer, default=0)
    files_synced: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)
    # Encrypted entries this instance's current key (and key history) couldn't
    # decrypt, or a file already present locally with matching content - not
    # failures, just nothing to do for that entry.
    files_skipped: Mapped[int] = mapped_column(Integer, default=0)
    bytes_synced: Mapped[int] = mapped_column(BigInteger, default=0)
    detail: Mapped[Optional[str]] = mapped_column(String)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    # JSON list of {"path", "status": "synced"|"failed"|"skipped", "error"?}.
    results_json: Mapped[Optional[str]] = mapped_column(Text)
    phase: Mapped[Optional[str]] = mapped_column(String)
    phase_done: Mapped[int] = mapped_column(BigInteger, default=0)
    phase_total: Mapped[int] = mapped_column(BigInteger, default=0)
    phase_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    celery_task_id: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class BackupSchedule(Base):
    """A recurring backup for one library, checked once a minute by the
    worker's check_due_schedules task (worker/app/scheduler.py) and fired
    through the same run_backup path as a manual "Back up" click. One
    schedule per library - source_storage_location_id is unique. next_run_at
    is precomputed (on save, and again after each firing) so the periodic
    check is a cheap "is anything due" query rather than recomputing
    everyone's schedule every minute."""

    __tablename__ = "backup_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_storage_location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # "daily" or "weekly".
    frequency: Mapped[str] = mapped_column(String, default="daily")
    # Time of day (container-local) the backup should run.
    time_of_day: Mapped[time] = mapped_column(Time)
    # Comma-separated weekday numbers, Monday=0..Sunday=6. Only meaningful
    # when frequency == "weekly".
    days_of_week: Mapped[Optional[str]] = mapped_column(String)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BackupKeyVersion(Base):
    """Every backup passphrase the app has used, so a rotated-out key is never
    lost. The row with retired_at NULL is the current key (it mirrors
    BackupEncryptionConfig.password/kdf_salt, which is what the worker reads).
    Stored plaintext, like the config row, so unattended restores work and so
    the user can retrieve an old key from Settings. Each v2 BackupArchive
    records the version that encrypted it (key_version_id) so restore can pick
    the right key after a rotation."""

    __tablename__ = "backup_key_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    password: Mapped[str] = mapped_column(String)
    kdf_salt: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class NotificationConfig(Base):
    """Singleton settings row (like TransferConfig/CloudStorageConfig) for the
    three notification channels - Settings > Notifications. Event-type
    toggles are shared across whichever channels are enabled, rather than
    configured per channel, by design (simpler; split later if ever needed).
    SMTP password and the Pushover token/key are stored plaintext, same
    tradeoff as the other secrets in this app (see CLAUDE.md)."""

    __tablename__ = "notification_config"

    id: Mapped[int] = mapped_column(primary_key=True)

    browser_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_host: Mapped[Optional[str]] = mapped_column(String)
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_username: Mapped[Optional[str]] = mapped_column(String)
    smtp_password: Mapped[Optional[str]] = mapped_column(String)
    smtp_from: Mapped[Optional[str]] = mapped_column(String)
    smtp_to: Mapped[Optional[str]] = mapped_column(String)

    pushover_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # The user's own Application/API Token (registered free at
    # pushover.net/apps/build) - deliberately not a token baked into this
    # app, so self-hosted instances don't share one account's rate limit.
    pushover_api_token: Mapped[Optional[str]] = mapped_column(String)
    pushover_user_key: Mapped[Optional[str]] = mapped_column(String)

    # Which events notify, applied to every enabled channel.
    notify_backup_done: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_backup_failed: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_restore_done: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_restore_failed: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class NotificationEvent(Base):
    """One notification-worthy occurrence, written at the moment it happens
    (worker/app/notify.py) - doubles as the feed the browser polls
    (GET /notifications/poll) and as an audit trail of what fired and when."""

    __tablename__ = "notification_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    level: Mapped[str] = mapped_column(String, default="info")  # info | warning | error
    title: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
