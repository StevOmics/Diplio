"""
Mirrors the subset of MediaBridge's shared schema this service touches.
The web service (web/app/models.py) owns table creation; keep column
definitions here in sync with it by hand.
"""
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
    location_type: Mapped[str] = mapped_column(String, default="local")
    media_type: Mapped[str] = mapped_column(String, default="movies")
    path: Mapped[str] = mapped_column(String, unique=True)
    is_backup_target: Mapped[bool] = mapped_column(Boolean, default=False)
    exclude_globs: Mapped[Optional[str]] = mapped_column(Text)
    # The archive (a backup-target StorageLocation, local or gcs) this library
    # backs up to, and an optional folder inside it. One archive per library.
    archive_location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="SET NULL"), nullable=True
    )
    archive_subpath: Mapped[Optional[str]] = mapped_column(String)
    storage_class: Mapped[Optional[str]] = mapped_column(String)
    # Mirrors web/app/models.py:StorageLocation.encrypted - NULL inherits the
    # global BackupEncryptionConfig setting, True/False overrides it per library.
    encrypted: Mapped[Optional[bool]] = mapped_column(Boolean)


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String, unique=True, index=True, default=lambda: str(uuid_lib.uuid4()))
    path: Mapped[str] = mapped_column(String, unique=True, index=True)
    filename: Mapped[str] = mapped_column(String)
    # Mirrors web/app/models.py's NOT NULL, client-default-only columns -
    # without these, an ORM INSERT of a fresh MediaFile row from worker code
    # (worker/app/sync_run.py) violates the real table's constraints. See
    # CloudStorageConfig's comment below for the same pattern.
    extension: Mapped[str] = mapped_column(String, default="")
    media_type: Mapped[str] = mapped_column(String, default="files")
    watched: Mapped[bool] = mapped_column(Boolean, default=False)
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    storage_location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("storage_locations.id"))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    fingerprint: Mapped[Optional[str]] = mapped_column(String)
    is_missing: Mapped[bool] = mapped_column(Boolean, default=False)


class BackupEncryptionConfig(Base):
    __tablename__ = "backup_encryption_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    password: Mapped[Optional[str]] = mapped_column(String)
    kdf_salt: Mapped[Optional[str]] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BackupArchive(Base):
    __tablename__ = "backup_archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # v2 backup pipeline (docs/cfa-spec.md sections 4/6.4) - all nullable; NULL
    # on all four together means a pre-v2 archive (legacy restore path).
    # indexed_at IS NULL marks an archive with no index file yet - incomplete,
    # and skipped by restore.
    archive_id: Mapped[Optional[str]] = mapped_column(String(36), unique=True, index=True)
    archive_type: Mapped[Optional[str]] = mapped_column(String)  # "clump" / "single" / "part"
    crc32c: Mapped[Optional[str]] = mapped_column(String)  # base64, as confirmed at upload
    indexed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Which BackupKeyVersion encrypted this v2 archive; NULL for plain and legacy
    # archives (restore then falls back to the current key).
    key_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("backup_key_versions.id", ondelete="SET NULL"), nullable=True)


class BackupRecord(Base):
    __tablename__ = "backup_records"
    __table_args__ = (
        UniqueConstraint("media_file_id", "destination_storage_location_id", name="uq_backup_record_media_file_destination"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"), index=True)
    destination_storage_location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), index=True
    )
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
    # NULL for as-is (every backup made before compression existed). Restore
    # and verify read this, never the current setting.
    compression: Mapped[Optional[str]] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="done")
    verify_status: Mapped[Optional[str]] = mapped_column(String)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BackupRecordArchive(Base):
    __tablename__ = "backup_record_archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    backup_record_id: Mapped[int] = mapped_column(ForeignKey("backup_records.id", ondelete="CASCADE"), index=True)
    backup_archive_id: Mapped[int] = mapped_column(ForeignKey("backup_archives.id", ondelete="CASCADE"), index=True)
    part_index: Mapped[int] = mapped_column(Integer, default=0)
    archive_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    archive_length: Mapped[int] = mapped_column(BigInteger)


class BackupRecordContentId(Base):
    """One row per (record, key-version) pair the record's HMAC content-id
    (encryption.derive_content_id) has been computed under. content_id
    replaces raw sha256 as the identifier exposed in GCS-visible artifacts
    (index JSON keys, encrypted tar member names) so a candidate file can't
    be hashed and checked against our archives without the master key. Rows
    accumulate across key rotations (backfilled cheaply from the already-known
    sha256, no file re-read) so dedup keeps recognizing old content after the
    key changes - see docs/backup-plan/DEVIATIONS.md."""

    __tablename__ = "backup_record_content_ids"
    __table_args__ = (
        UniqueConstraint("backup_record_id", "key_version_id", name="uq_content_id_record_key_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    backup_record_id: Mapped[int] = mapped_column(ForeignKey("backup_records.id", ondelete="CASCADE"), index=True)
    key_version_id: Mapped[int] = mapped_column(ForeignKey("backup_key_versions.id", ondelete="CASCADE"), index=True)
    content_id: Mapped[str] = mapped_column(String(64), index=True)


class TransferConfig(Base):
    __tablename__ = "transfer_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    min_size_bytes: Mapped[int] = mapped_column(BigInteger, default=10485760)  # 10 MiB
    clump_size_bytes: Mapped[int] = mapped_column(BigInteger, default=67108864)  # 64 MiB
    max_size_bytes: Mapped[int] = mapped_column(BigInteger, default=1073741824)  # 1 GiB
    # web-only logic (splits a big backup into several BackupRuns before
    # enqueueing - see web/app/main.py:_start_batched_backup_runs); mirrored
    # here only because the real table has it NOT NULL - without this, an
    # ORM INSERT of a fresh TransferConfig row from worker code violates
    # that constraint (see CloudStorageConfig's comment below for the same
    # pattern).
    batch_bytes: Mapped[int] = mapped_column(BigInteger, default=5368709120)  # 5 GiB
    # Optional gzip of each file before encryption (see compression.py). Files
    # that don't shrink enough are stored as-is regardless.
    compression_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    compression_level: Mapped[int] = mapped_column(Integer, default=6)
    # Hard ceiling on upload speed, in Mbps - see app/gcs.py:effective_upload_mbps.
    max_upload_mbps: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CloudStorageConfig(Base):
    __tablename__ = "cloud_storage_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Mirrors web/app/models.py's NOT NULL columns - without these, anything
    # in the worker (or its tests) that INSERTs a row violates the real table's
    # constraints.
    provider: Mapped[str] = mapped_column(String, default="gcs")
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    is_backup_target: Mapped[bool] = mapped_column(Boolean, default=False)
    service_account_json: Mapped[Optional[str]] = mapped_column(Text)
    project_id: Mapped[Optional[str]] = mapped_column(String)
    bucket_name: Mapped[Optional[str]] = mapped_column(String)
    prefix: Mapped[Optional[str]] = mapped_column(String)
    upload_mbps: Mapped[Optional[float]] = mapped_column(Float)


class S3StorageConfig(Base):
    """Mirrors web/app/models.py:S3StorageConfig - the worker reads this
    directly to build an S3Backend for an s3:// archive."""

    __tablename__ = "s3_storage_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    access_key_id: Mapped[Optional[str]] = mapped_column(String)
    secret_access_key: Mapped[Optional[str]] = mapped_column(String)
    region: Mapped[Optional[str]] = mapped_column(String)
    bucket_name: Mapped[Optional[str]] = mapped_column(String)
    prefix: Mapped[Optional[str]] = mapped_column(String)


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
    """Mirrors web/app/models.py's SyncRun - see there for the full docstring.
    Written to by worker/app/sync_run.py."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    library_storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"))
    archive_storage_location_id: Mapped[int] = mapped_column(ForeignKey("storage_locations.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String, default="queued")
    files_total: Mapped[int] = mapped_column(Integer, default=0)
    files_synced: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)
    files_skipped: Mapped[int] = mapped_column(Integer, default=0)
    bytes_synced: Mapped[int] = mapped_column(BigInteger, default=0)
    detail: Mapped[Optional[str]] = mapped_column(String)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
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
    """Mirrors web/app/models.py's BackupSchedule - see there for the full
    docstring. Checked once a minute by check_due_schedules (app/scheduler.py)."""

    __tablename__ = "backup_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_storage_location_id: Mapped[int] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency: Mapped[str] = mapped_column(String, default="daily")
    time_of_day: Mapped[time] = mapped_column(Time)
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
    """Mirrors web/app/models.py:NotificationConfig - the worker reads this
    directly to send email/Pushover at the moment a run finishes."""

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
    pushover_api_token: Mapped[Optional[str]] = mapped_column(String)
    pushover_user_key: Mapped[Optional[str]] = mapped_column(String)
    notify_backup_done: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_backup_failed: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_restore_done: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_restore_failed: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class NotificationEvent(Base):
    """Mirrors web/app/models.py:NotificationEvent - the worker inserts a row
    here (for the browser feed/audit trail) whenever it sends a notification."""

    __tablename__ = "notification_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(String, default="info")
    title: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
