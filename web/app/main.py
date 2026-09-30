import json
import logging
from types import SimpleNamespace
import re
import secrets
import threading
from datetime import datetime, time as time_cls, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth import hash_password, verify_password
from app.backup_settings import MIB, parse_exclude_globs, parse_upload_limit, validate_archive_sizes
from app.catalog import _map_jellyfin_path, _scan_location, scan_library, sync_watch_data
from app.config import MEDIA_TYPE_EXTENSIONS, MEDIA_TYPES, settings as app_settings
from app.db import Base, SessionLocal, engine, get_db
from app import gcs, jellyfin, tls
from app import s3
from app.encryption_check import all_candidate_keys, check_key_match, derive_master_key
from app import models  # noqa: F401  (registers tables with Base.metadata)
from app.models import (
    BackupEncryptionConfig,
    BackupRecord,
    BackupArchive,
    BackupRecordArchive,
    BackupRun,
    BackupSchedule,
    RestoreRun,
    SyncRun,
    CloudStorageConfig,
    JellyfinConfig,
    MediaFile,
    NotificationConfig,
    NotificationEvent,
    S3StorageConfig,
    StorageLocation,
    TlsConfig,
    TransferConfig,
    User,
)
from app.schedule_utils import VALID_FREQUENCIES, compute_next_run, parse_days_of_week
from app.archive_paths import (
    format_gcs_path,
    format_s3_path,
    is_s3_path,
    normalize_prefix,
    parse_gcs_path,
    parse_s3_path,
)
from app.cloud_inventory import build_report
from app.key_versions import backfill_content_ids, ensure_current_version, kdf_salt_is_valid_hex, key_history, set_passphrase
from app.schemas import MediaFileOut
from app.tasks_client import celery_client, enqueue_restore_run, enqueue_run_backup, enqueue_sync_run, enqueue_verify_batches

TERMINATOR_URL = "http://terminator:8000"

# Must match worker/app/gcs.py's UPLOAD_THROTTLE_FRACTION/DEFAULT_UPLOAD_CAP_MBPS
# - real cloud uploads are capped to this same rate so a backup doesn't
# saturate the connection; ETA estimates here use it too.
CLOUD_UPLOAD_THROTTLE_FRACTION = 0.5
DEFAULT_UPLOAD_CAP_MBPS = 15.0


def _effective_upload_mbps(upload_mbps: float | None, max_upload_mbps: float | None) -> float:
    """Mirrors worker/app/gcs.py's effective_upload_mbps: the user's explicit
    TransferConfig.max_upload_mbps if set (a hard ceiling, regardless of
    measured speed), else CLOUD_UPLOAD_THROTTLE_FRACTION of the last speed
    test, else DEFAULT_UPLOAD_CAP_MBPS before any test has run. Always a
    number - used both for the Settings display and the backup-time ETA."""
    if max_upload_mbps:
        return max_upload_mbps
    if upload_mbps:
        return upload_mbps * CLOUD_UPLOAD_THROTTLE_FRACTION
    return DEFAULT_UPLOAD_CAP_MBPS


app = FastAPI(title="MediaBridge")
# Only ever reached via the nginx reverse proxy on the Docker network, so
# X-Forwarded-Proto is trusted here to mark the session cookie Secure -
# without this, the cookie would need `secure=False` and ride in cleartext
# on the trusted-network hop between nginx and this container.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.add_middleware(SessionMiddleware, secret_key=app_settings.secret_key, https_only=True)
templates = Jinja2Templates(directory="app/templates")
# Starlette's Jinja2Templates doesn't register Flask's `tojson` filter - needed
# to safely embed a server-side value (e.g. media_root) inside inline <script>
# blocks (settings.html's browse-folders dialog). Markup-wrapped so autoescape
# doesn't then HTML-escape the JSON string's quotes.
templates.env.filters["tojson"] = lambda value: Markup(json.dumps(value).replace("<", "\\u003c"))


class NotAuthenticated(Exception):
    pass


@app.exception_handler(NotAuthenticated)
def not_authenticated_handler(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def require_login(request: Request) -> str:
    username = request.session.get("username")
    if not username:
        raise NotAuthenticated()
    return username


def require_admin(request: Request, db: Session = Depends(get_db)) -> str:
    username = require_login(request)
    user = db.query(User).filter_by(username=username).one_or_none()
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return username


def _migrate_library_archives(db: Session, assign_defaults: bool) -> None:
    """One-time, idempotent move from "one implicit cloud bucket + one implicit
    local target" to explicit per-library archives:
    - a cloud archive stored as gcs://bucket becomes gs://bucket[/prefix] (folding
      in the old CloudStorageConfig.prefix), the canonical archive path;
    - only the first time (assign_defaults, i.e. the column was just added): any
      local library is pointed at the existing archive when exactly one exists
      per kind (cloud preferred), so nothing that backed up before stops
      working. Never re-run - a library the user later sets to "no archive"
      must stay that way."""
    config = db.query(CloudStorageConfig).first()
    for loc in db.query(StorageLocation).filter_by(location_type="gcs").all():
        if loc.path.lower().startswith("gcs://"):
            bucket = loc.path[len("gcs://"):].split("/", 1)[0]
            prefix = config.prefix if config and config.bucket_name == bucket else ""
            loc.path = format_gcs_path(bucket, prefix or "")
    db.commit()

    if not assign_defaults:
        return
    cloud = db.query(StorageLocation).filter_by(location_type="gcs", is_backup_target=True).all()
    local = db.query(StorageLocation).filter_by(location_type="local", is_backup_target=True).all()
    default = cloud[0] if len(cloud) == 1 else (local[0] if len(local) == 1 else None)
    if default is not None:
        db.query(StorageLocation).filter(
            StorageLocation.location_type == "local",
            StorageLocation.is_backup_target.is_(False),
            StorageLocation.archive_location_id.is_(None),
        ).update({"archive_location_id": default.id}, synchronize_session=False)
        db.commit()


def _add_missing_columns(db: Session) -> list[str]:
    """Safety net for the no-migration-tool setup: adds any model column that an
    existing table lacks, so a column added to models.py can't take down every
    page that reads the table (as transfer_config.internet_speed_mbps did on
    databases created by an older version). Explicit ALTERs above stay the
    place to choose proper defaults; this only fills gaps they missed.

    Additive only. A NOT NULL column is added NOT NULL only when it has a plain
    scalar Python default (used as the SQL DEFAULT); otherwise it is added
    nullable rather than failing on existing rows. Foreign keys and indexes are
    not created here. Returns the "table.column" names it added."""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing or col.primary_key:
                continue
            sql_type = col.type.compile(dialect=engine.dialect)
            default = col.default.arg if col.default is not None and col.default.is_scalar else None
            clause = f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS "{col.name}" {sql_type}'
            if default is not None and not col.nullable:
                literal = "true" if default is True else "false" if default is False else (
                    "'" + str(default).replace("'", "''") + "'" if isinstance(default, str) else str(default)
                )
                clause += f" NOT NULL DEFAULT {literal}"
            db.execute(text(clause))
            added.append(f"{table.name}.{col.name}")
    if added:
        db.commit()
        print(f"startup: added missing columns {added}", flush=True)
    return added


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # create_all only creates missing tables, not missing columns on
        # existing ones - this repo has no migration tool (see CLAUDE.md), so
        # a newly added column on an already-created table needs an explicit,
        # idempotent guard like this rather than relying on create_all alone.
        db.execute(text("ALTER TABLE storage_locations ADD COLUMN IF NOT EXISTS media_type VARCHAR NOT NULL DEFAULT 'movies'"))
        db.execute(text("ALTER TABLE media_files ADD COLUMN IF NOT EXISTS is_missing BOOLEAN NOT NULL DEFAULT false"))
        db.execute(text("ALTER TABLE media_files ADD COLUMN IF NOT EXISTS mtime_ns BIGINT"))
        db.execute(text("ALTER TABLE storage_locations ADD COLUMN IF NOT EXISTS untracked BOOLEAN NOT NULL DEFAULT false"))
        db.execute(text("ALTER TABLE storage_locations ADD COLUMN IF NOT EXISTS encrypted BOOLEAN"))
        db.execute(text("ALTER TABLE cloud_storage_config ADD COLUMN IF NOT EXISTS last_inventory_json TEXT"))
        db.execute(text("ALTER TABLE cloud_storage_config ADD COLUMN IF NOT EXISTS last_inventory_at TIMESTAMPTZ"))
        db.execute(text("ALTER TABLE backup_runs ADD COLUMN IF NOT EXISTS backed_up_json TEXT"))
        db.commit()
        # v2 backup pipeline settings (docs/cfa-spec.md section 2) - additive only, see
        # docs/backup-plan/steps/01-settings.md. Nothing reads these yet.
        db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS min_size_bytes BIGINT NOT NULL DEFAULT 2621440"))
        db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS clump_size_bytes BIGINT NOT NULL DEFAULT 67108864"))
        db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS max_size_bytes BIGINT NOT NULL DEFAULT 1073741824"))
        db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS compression_enabled BOOLEAN NOT NULL DEFAULT false"))
        db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS compression_level INTEGER NOT NULL DEFAULT 6"))
        db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS batch_bytes BIGINT NOT NULL DEFAULT 5368709120"))
        db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS max_upload_mbps DOUBLE PRECISION"))
        db.execute(text("ALTER TABLE backup_records ADD COLUMN IF NOT EXISTS compression VARCHAR"))
        db.execute(text("ALTER TABLE backup_runs ADD COLUMN IF NOT EXISTS phase VARCHAR"))
        db.execute(text("ALTER TABLE backup_runs ADD COLUMN IF NOT EXISTS phase_done BIGINT NOT NULL DEFAULT 0"))
        db.execute(text("ALTER TABLE backup_runs ADD COLUMN IF NOT EXISTS phase_total BIGINT NOT NULL DEFAULT 0"))
        db.execute(text("ALTER TABLE backup_runs ADD COLUMN IF NOT EXISTS phase_started_at TIMESTAMPTZ"))
        db.execute(text("ALTER TABLE backup_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ"))
        db.execute(text("ALTER TABLE cloud_storage_config ADD COLUMN IF NOT EXISTS prefix VARCHAR"))
        db.execute(text("ALTER TABLE storage_locations ADD COLUMN IF NOT EXISTS exclude_globs TEXT"))
        db.execute(text("ALTER TABLE storage_locations ADD COLUMN IF NOT EXISTS storage_class VARCHAR"))
        db.execute(text("ALTER TABLE storage_locations ADD COLUMN IF NOT EXISTS discover_report_json TEXT"))
        db.commit()
        # v2 backup pipeline (docs/cfa-spec.md sections 1.2/6.2) - per-backup
        # SHA-256 and the mtime_ns it was computed at. See
        # docs/backup-plan/steps/02-sha256.md. Nothing writes these yet.
        db.execute(text("ALTER TABLE backup_records ADD COLUMN IF NOT EXISTS sha256 VARCHAR(64)"))
        db.execute(text("ALTER TABLE backup_records ADD COLUMN IF NOT EXISTS mtime_ns BIGINT"))
        db.execute(text("CREATE INDEX IF NOT EXISTS ix_backup_records_sha256 ON backup_records (sha256)"))
        db.commit()
        # v2 backup pipeline (docs/cfa-spec.md sections 4/6.4) - archive
        # identity and upload-verification columns. See
        # docs/backup-plan/steps/05-storage-and-upload.md. Nothing writes
        # these yet. NULL on all four means a pre-v2 archive, handled by the
        # legacy restore path; the unique index tolerates any number of those
        # NULLs while forcing every v2 archive_id to be distinct.
        db.execute(text("ALTER TABLE backup_archives ADD COLUMN IF NOT EXISTS archive_id VARCHAR(36)"))
        db.execute(text("ALTER TABLE backup_archives ADD COLUMN IF NOT EXISTS archive_type VARCHAR"))
        db.execute(text("ALTER TABLE backup_archives ADD COLUMN IF NOT EXISTS crc32c VARCHAR"))
        db.execute(text("ALTER TABLE backup_archives ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ"))
        db.execute(text("ALTER TABLE backup_archives ADD COLUMN IF NOT EXISTS key_version_id INTEGER"))
        # Per-library archives (Libraries page): which archive a library backs up to,
        # and an optional folder inside it. Existing installs had exactly one cloud
        # bucket and one local target used implicitly for every library, so
        # migrate that into explicit assignments below.
        first_archive_migration = not db.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'storage_locations' AND column_name = 'archive_location_id'"
            )
        ).first()
        db.execute(text("ALTER TABLE storage_locations ADD COLUMN IF NOT EXISTS archive_location_id INTEGER"))
        db.execute(text("ALTER TABLE storage_locations ADD COLUMN IF NOT EXISTS archive_subpath VARCHAR"))
        # "replace older" vs "replace all" mode for backup/restore runs.
        db.execute(text("ALTER TABLE backup_runs ADD COLUMN IF NOT EXISTS mode VARCHAR(20) NOT NULL DEFAULT 'replace_older'"))
        db.execute(text("ALTER TABLE restore_runs ADD COLUMN IF NOT EXISTS mode VARCHAR(20) NOT NULL DEFAULT 'replace_older'"))
        db.execute(text("ALTER TABLE tls_config ADD COLUMN IF NOT EXISTS domains TEXT"))
        db.execute(text("ALTER TABLE tls_config ADD COLUMN IF NOT EXISTS redirect_http BOOLEAN NOT NULL DEFAULT true"))
        db.execute(text("ALTER TABLE tls_config ADD COLUMN IF NOT EXISTS external_url VARCHAR"))
        db.commit()
        _add_missing_columns(db)  # before any ORM query touches a table an older DB may lack columns on
        _migrate_library_archives(db, assign_defaults=first_archive_migration)
        db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_backup_archives_archive_id ON backup_archives (archive_id)"))
        db.commit()
        # The legacy per-file backup pipeline is gone (v2 is the only strategy):
        # drop what only it used. Idempotent - a no-op once already dropped.
        db.execute(text("DROP TABLE IF EXISTS copy_jobs"))
        for column in (
            "max_speed_mbps", "max_size_gb", "split_over_percent", "min_size_mb", "clump_under_percent",
            "clump_split_enabled", "retry_count", "retry_interval_minutes", "internet_speed_mbps",
            "measure_speed_on_transfer",
        ):
            db.execute(text(f"ALTER TABLE transfer_config DROP COLUMN IF EXISTS {column}"))
        for column in ("checksum", "encryption_key_id", "is_clump"):
            db.execute(text(f"ALTER TABLE backup_archives DROP COLUMN IF EXISTS {column}"))
        db.commit()
        # One-time rename from this column's original singular/no-"files"
        # vocabulary (movie/photo/audio/document/mixed) to the current one
        # (movies/photos/music/documents/files) - harmless no-op once no rows
        # still hold an old value.
        rename_media_types = {"movie": "movies", "photo": "photos", "audio": "music", "document": "documents", "mixed": "files"}
        for old, new in rename_media_types.items():
            db.execute(
                text("UPDATE storage_locations SET media_type = :new WHERE media_type = :old"), {"old": old, "new": new}
            )
            db.execute(text("UPDATE media_files SET media_type = :new WHERE media_type = :old"), {"old": old, "new": new})
        db.commit()

        # Deploy-time backfill for installs that already had backups before
        # content-id keying existed: populates BackupRecordContentId for the
        # current key version across every existing record, the same
        # mechanism a later rotation uses (key_versions.backfill_content_ids)
        # - so dedup recognizes pre-existing content immediately instead of
        # only after each file happens to be re-touched by a future backup.
        # Cheap (one HMAC per record, no file/bucket access) and safe to
        # re-run - it skips records already covered.
        backup_encryption_config = db.query(BackupEncryptionConfig).first()
        if backup_encryption_config and backup_encryption_config.password and backup_encryption_config.kdf_salt:
            current_version = ensure_current_version(db, backup_encryption_config)
            if current_version:
                backfill_content_ids(
                    db,
                    current_version.id,
                    derive_master_key(backup_encryption_config.password, bytes.fromhex(backup_encryption_config.kdf_salt)),
                )
            db.commit()

        jellyfin_config = db.query(JellyfinConfig).first()

        # Regenerate the nginx server-block file from the DB on every startup
        # so it can't drift from TlsConfig (e.g. after the generated file's
        # volume was reset). This only rewrites the file - it doesn't restart
        # nginx, which reads it at its own container start.
        tls_config = _get_tls_config(db)
        tls.write_nginx_config(tls_config.domains or "", tls_config.redirect_http)
    finally:
        db.close()

    if jellyfin_config:
        # Warms jellyfin's cached user/library lookups in the background so the
        # first Settings page load after a restart doesn't block on them too.
        for target in (jellyfin.list_users_cached, jellyfin.list_libraries_cached):
            threading.Thread(
                target=target,
                args=(jellyfin_config.server_url, jellyfin_config.api_key),
                daemon=True,
            ).start()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/db")
def health_db(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


def _get_system_health(db: Session) -> dict:
    """Gather system health status: database connectivity, celery workers, current time."""
    health = {
        "database_ok": False,
        "database_error": None,
        "celery_ok": False,
        "celery_workers": 0,
        "celery_tasks_active": 0,
        "current_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    # Check database
    try:
        db.execute(text("SELECT 1"))
        health["database_ok"] = True
    except Exception as e:
        health["database_error"] = str(e)

    # Check Celery workers
    try:
        inspect = celery_client.control.inspect()
        stats = inspect.stats()
        if stats:
            health["celery_ok"] = True
            health["celery_workers"] = len(stats)
            # Count active tasks across all workers
            active = inspect.active()
            if active:
                health["celery_tasks_active"] = sum(len(tasks) for tasks in active.values())
    except Exception:
        health["celery_ok"] = False

    return health


def _get_activity_summary(db: Session) -> dict:
    """Counts of queued, running, and failed backup/restore runs."""

    def count(status: str) -> int:
        return db.query(BackupRun).filter_by(status=status).count() + db.query(RestoreRun).filter_by(status=status).count()

    return {"pending_count": count("queued"), "running_count": count("running"), "failed_count": count("failed")}


def _format_bytes(num_bytes: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024:
            if unit == "B":
                return f"{num_bytes} {unit}"
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def _get_storage_summary(db: Session) -> dict:
    """Get catalog storage statistics and backup target info."""
    total_files = db.query(func.count(MediaFile.id)).scalar() or 0
    total_bytes = db.query(func.sum(MediaFile.size_bytes)).scalar() or 0

    # Get breakdown by media type
    by_type = db.query(
        MediaFile.media_type,
        func.count(MediaFile.id),
        func.sum(MediaFile.size_bytes)
    ).group_by(MediaFile.media_type).order_by(MediaFile.media_type).all()

    by_media_type = [
        (media_type, count or 0, _format_bytes(size_bytes or 0))
        for media_type, count, size_bytes in by_type
    ]

    cloud_backup_target = _get_cloud_backup_location(db)

    return {
        "total_files": total_files,
        "total_size_formatted": _format_bytes(total_bytes),
        "by_media_type": by_media_type,
        "cloud_backup_target": cloud_backup_target,
    }


def _get_configuration_summary(db: Session) -> dict:
    """Get configuration settings for display on the status page."""
    transfer_config = db.query(TransferConfig).first()
    encryption_config = db.query(BackupEncryptionConfig).first()
    jellyfin_config = db.query(JellyfinConfig).first()
    cloud_storage_config = db.query(CloudStorageConfig).first()

    transfer_summary = None
    if transfer_config:
        transfer_summary = {
            "min_size_formatted": _format_bytes(transfer_config.min_size_bytes),
            "clump_size_formatted": _format_bytes(transfer_config.clump_size_bytes),
            "max_size_formatted": _format_bytes(transfer_config.max_size_bytes),
        }

    encryption_summary = {
        "enabled": encryption_config.enabled if encryption_config else False,
    }

    return {
        "transfer_config": transfer_summary,
        "encryption_config": encryption_summary,
        "jellyfin_config": jellyfin_config,
        "cloud_storage_config": cloud_storage_config,
    }


@app.post("/system/cloud-inventory", dependencies=[Depends(require_login)])
def refresh_cloud_inventory(db: Session = Depends(get_db)):
    """Lists the objects MediaBridge created in the cloud archive's bucket
    folder, with their storage tier, and compares them with the database.
    Read-only; one metadata-only listing, run only when the user asks."""
    destination = _get_cloud_backup_location(db)
    config = _get_cloud_storage_config(db)
    if not destination or not config or not config.service_account_json:
        return JSONResponse({"error": "No cloud backup target is configured"}, status_code=400)
    try:
        bucket, prefix = parse_gcs_path(destination.path)
        objects = gcs.list_objects(config.service_account_json, bucket, prefix, config.project_id)
    except Exception as exc:  # noqa: BLE001 - shown to the user, not fatal
        return JSONResponse({"error": f"Couldn't list the bucket: {exc}"}, status_code=502)
    known = {
        path
        for (path,) in db.query(BackupArchive.path).filter(
            BackupArchive.storage_location_id == destination.id, BackupArchive.archive_id.isnot(None)
        )
        if path.startswith(prefix)
    }
    report = build_report(objects, known)
    checked_at = datetime.now(timezone.utc)
    result = {
        "checked_at": checked_at.strftime("%Y-%m-%d %H:%M UTC"),
        "location": f"gs://{bucket}/{prefix}",
        "archive_count": report.archive_count,
        "total_size": _format_bytes(report.total_bytes),
        "tiers": [
            {"tier": tier, "count": count, "size": _format_bytes(size)}
            for tier, (count, size) in sorted(report.by_tier.items())
        ],
        "untracked": report.untracked,
        "missing": report.missing,
        "orphan_index": report.orphan_index,
        "no_index": report.no_index,
    }
    # Cached so System Status still shows this after a reload instead of
    # reverting to "Not checked yet." - see CloudStorageConfig.last_inventory_json.
    config.last_inventory_json = json.dumps(result)
    config.last_inventory_at = checked_at
    db.commit()
    return result


def _discover_bucket_report(db: Session, library: StorageLocation, archive: StorageLocation) -> dict:
    """Lists this library's own folder in its assigned archive's bucket and
    reports whether there's already-backed-up content there that this
    (fresh, or repointed) instance's database doesn't know about yet - the
    portability scenario: a new install, pointed at an old bucket. Read-only,
    metadata listing plus a cheap decrypt attempt against each index.json
    found there (never a tar payload) to report whether this instance's
    configured master key actually opens that content - see
    /libraries/{location_id}/sync-from-bucket for the part that reads paths
    out of the index files and actually restores anything.
    docs/backup-plan/steps/18-portability.md. Returns {"error": ...} on any
    failure, otherwise the report shape the Libraries page JS expects."""
    config = _get_cloud_storage_config(db)
    if not config or not config.service_account_json:
        return {"error": "No Google Cloud service account is configured"}
    try:
        bucket, base = parse_gcs_path(archive.path)
    except ValueError:
        return {"error": "Discovery only supports Google Cloud Storage archives so far"}
    subpath = library.archive_subpath if library.archive_location_id == archive.id else ""
    prefix = normalize_prefix(base, subpath)

    try:
        objects = gcs.list_objects(config.service_account_json, bucket, prefix, config.project_id)
    except Exception as exc:  # noqa: BLE001 - shown to the user, not fatal
        return {"error": f"Couldn't list the bucket: {exc}"}

    known = {
        path
        for (path,) in db.query(BackupArchive.path).filter(
            BackupArchive.storage_location_id == archive.id, BackupArchive.archive_id.isnot(None)
        )
        if path.startswith(prefix)
    }
    report = build_report(objects, known)
    untracked = set(report.untracked)
    result = {
        "location": f"gs://{bucket}/{prefix}",
        "archive_count": report.archive_count,
        "total_size": _format_bytes(report.total_bytes),
        # What matters for portability: archives that are in the bucket at
        # this library's own prefix but this database has never recorded -
        # i.e. content from another instance (or a database that was reset).
        "found_count": len(report.untracked),
        "found_size": _format_bytes(sum(size for key, size, _ in objects if key in untracked)),
        "no_index_count": len(report.no_index),
    }
    if untracked:
        index_keys = [key for key, _, _ in objects if key.endswith(".json")]
        candidate_keys = all_candidate_keys(db)
        key_status = check_key_match(
            index_keys, lambda key: gcs.read_object(config.service_account_json, bucket, key, config.project_id), candidate_keys
        )
        key_messages = {
            "match": "This content was saved with the currently configured master key.",
            "no_match": "These files don't appear to have been saved with the currently configured master key.",
            "mixed": "Some of these files were saved with the currently configured master key, but not all of them.",
            "unencrypted": "This content wasn't encrypted.",
            "no_content": "",
        }
        result["key_status"] = key_status
        result["key_message"] = key_messages[key_status]
    return result


@app.post("/libraries/{location_id}/discover-bucket", dependencies=[Depends(require_login)])
def discover_bucket_contents(location_id: int, db: Session = Depends(get_db)):
    library = db.get(StorageLocation, location_id)
    archive = _archive_for_location_id(db, location_id)
    if not library or not archive:
        return JSONResponse({"error": "This library has no archive assigned"}, status_code=400)
    result = _discover_bucket_report(db, library, archive)
    if "error" in result:
        status_code = 502 if "Couldn't list" in result["error"] else 400
        return JSONResponse(result, status_code=status_code)
    library.discover_report_json = json.dumps(result)
    db.commit()
    return result


def _start_sync_run(db: Session, library: StorageLocation, archive: StorageLocation) -> SyncRun:
    """Creates the tracking row (shows on Backups & Restores immediately, as
    "queued") and hands it to the worker's sync_run."""
    run = SyncRun(library_storage_location_id=library.id, archive_storage_location_id=archive.id, status="queued")
    db.add(run)
    db.commit()
    try:
        run.celery_task_id = enqueue_sync_run(run.id)
        db.commit()
    except Exception as exc:  # broker down: don't leave a "queued" run that nothing will pick up
        run.status = "failed"
        run.error_message = f"could not queue the sync: {exc}"[:500]
        db.commit()
    return run


@app.post("/libraries/{location_id}/sync-from-bucket", dependencies=[Depends(require_login)])
def sync_library_from_bucket(location_id: int, db: Session = Depends(get_db)):
    """Starts a SyncRun: reads this library's archive folder's index files
    directly, and for anything found that this database doesn't already have
    a done backup record for, restores it into the library and records it as
    backed up - see /libraries/{location_id}/discover-bucket (the read-only
    check this follows) and docs/backup-plan/steps/18-portability.md."""
    library = db.get(StorageLocation, location_id)
    archive = _archive_for_location_id(db, location_id)
    if not library or not archive:
        return JSONResponse({"error": "This library has no archive assigned"}, status_code=400)
    try:
        parse_gcs_path(archive.path)
    except ValueError:
        return JSONResponse({"error": "Sync only supports Google Cloud Storage archives so far"}, status_code=400)
    run = _start_sync_run(db, library, archive)
    if run.status == "failed":
        return JSONResponse({"error": run.error_message}, status_code=502)
    return {"run_id": run.id}


@app.get("/system", response_class=HTMLResponse)
def system_status_page(
    request: Request,
    error: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
    username: str = Depends(require_login),
):
    """System status dashboard: health, activity, storage, and configuration overview."""
    health = _get_system_health(db)
    activity = _get_activity_summary(db)
    storage = _get_storage_summary(db)
    config = _get_configuration_summary(db)
    cloud_storage_config = config["cloud_storage_config"]
    cloud_inventory_json = None
    if cloud_storage_config and cloud_storage_config.last_inventory_json:
        # Embedded directly into a <script> block below - escape "</" so an
        # object/prefix name containing it (bucket contents, not fully trusted
        # input) can't break out of the script context.
        cloud_inventory_json = cloud_storage_config.last_inventory_json.replace("</", "<\\/")

    return templates.TemplateResponse(
        request,
        "system.html",
        {
            "active": "system",
            "health": health,
            "activity": activity,
            "storage": storage,
            "config": config,
            "cloud_inventory_json": cloud_inventory_json,
            "error": error,
            "message": message,
            "username": username,
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter_by(username=username).one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid username or password"}, status_code=401
        )
    request.session["username"] = user.username
    request.session["is_admin"] = user.is_admin
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/users", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def users_page(
    request: Request,
    error: str | None = None,
    flash_status: str | None = None,
    flash_message: str | None = None,
    db: Session = Depends(get_db),
    username: str = Depends(require_login),
):
    users = db.query(User).order_by(User.username).all()
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "active": "users",
            "users": users,
            "username": username,
            "error": error,
            "flash_status": flash_status,
            "flash_message": flash_message,
        },
    )


@app.post("/users", dependencies=[Depends(require_admin)])
def create_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    db: Session = Depends(get_db),
):
    username = username.strip()
    if not username or not password:
        return RedirectResponse(
            url="/users?flash_status=error&flash_message=Username+and+password+are+required", status_code=303
        )
    existing = db.query(User).filter_by(username=username).one_or_none()
    if existing:
        return RedirectResponse(
            url="/users?flash_status=error&flash_message=That+username+already+exists", status_code=303
        )
    user = User(username=username, password_hash=hash_password(password), is_admin=(role == "admin"))
    db.add(user)
    db.commit()
    return RedirectResponse(url="/users?flash_status=ok&flash_message=User+created", status_code=303)


@app.post("/users/{user_id}/role", dependencies=[Depends(require_admin)])
def update_user_role(
    user_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter_by(id=user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    make_admin = role == "admin"
    if user.is_admin and not make_admin:
        remaining_admins = db.query(func.count(User.id)).filter(User.is_admin.is_(True), User.id != user_id).scalar()
        if not remaining_admins:
            return RedirectResponse(
                url="/users?flash_status=error&flash_message=Cannot+demote+the+only+remaining+admin",
                status_code=303,
            )
    user.is_admin = make_admin
    db.commit()
    return RedirectResponse(url="/users?flash_status=ok&flash_message=Role+updated", status_code=303)


@app.post("/users/{user_id}/delete", dependencies=[Depends(require_admin)])
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter_by(id=user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        remaining_admins = db.query(func.count(User.id)).filter(User.is_admin.is_(True), User.id != user_id).scalar()
        if not remaining_admins:
            return RedirectResponse(
                url="/users?flash_status=error&flash_message=Cannot+delete+the+only+remaining+admin",
                status_code=303,
            )
    db.delete(user)
    db.commit()
    return RedirectResponse(url="/users?flash_status=ok&flash_message=User+deleted", status_code=303)


@app.get("/profile", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def profile_page(
    request: Request,
    error: str | None = None,
    flash_status: str | None = None,
    flash_message: str | None = None,
    db: Session = Depends(get_db),
    username: str = Depends(require_login),
):
    user = db.query(User).filter_by(username=username).one_or_none()
    if not user:
        raise NotAuthenticated()
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "active": "profile",
            "user": user,
            "username": username,
            "error": error,
            "flash_status": flash_status,
            "flash_message": flash_message,
        },
    )


@app.post("/profile", dependencies=[Depends(require_login)])
def update_profile(
    request: Request,
    new_username: str = Form(...),
    password: str = Form(""),
    db: Session = Depends(get_db),
    username: str = Depends(require_login),
):
    user = db.query(User).filter_by(username=username).one_or_none()
    if not user:
        raise NotAuthenticated()
    new_username = new_username.strip()
    if not new_username:
        return RedirectResponse(url="/profile?error=Username+cannot+be+empty", status_code=303)
    if new_username != user.username:
        existing = db.query(User).filter_by(username=new_username).one_or_none()
        if existing:
            return RedirectResponse(url="/profile?error=That+username+already+exists", status_code=303)
        user.username = new_username
    if password:
        user.password_hash = hash_password(password)
    db.commit()
    request.session["username"] = user.username
    return RedirectResponse(url="/profile?flash_status=ok&flash_message=Saved", status_code=303)


@app.post("/scan", dependencies=[Depends(require_login)])
def scan(db: Session = Depends(get_db)) -> dict:
    return scan_library(db)


# Browser-renderable via a plain <img> tag - excludes raw/heic formats
# Catalog's photos type also accepts (cr2, nef, dng, heic, heif) which most
# browsers can't decode inline, so those fall back to the generic icon.
_THUMBNAIL_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}


@app.get("/movies/{media_file_id}/thumbnail", dependencies=[Depends(require_login)])
def media_file_thumbnail(media_file_id: int, db: Session = Depends(get_db)):
    """Serves a photo's own file as its Catalog thumbnail. Only for the photos
    media type and a browser-safe extension; the Catalog's Thumbnails view
    falls back to a generic icon for anything else (movies have no poster art
    today) rather than calling this and getting a 404 per card."""
    media_file = db.query(MediaFile).filter_by(id=media_file_id).one_or_none()
    if (
        not media_file
        or media_file.media_type != "photos"
        or media_file.extension not in _THUMBNAIL_EXTENSIONS
        or media_file.is_missing
        or not Path(media_file.path).is_file()
    ):
        raise HTTPException(status_code=404)
    return FileResponse(media_file.path)


@app.get("/movies", response_model=list[MediaFileOut], dependencies=[Depends(require_login)])
def list_movies(
    genre: str | None = None,
    library: str | None = None,
    q: str | None = None,
    media_type: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(MediaFile)
    if media_type:
        query = query.filter(MediaFile.media_type == media_type)
    if genre:
        query = query.filter(MediaFile.genre == genre)
    if library:
        # "Library" is the storage location a file lives in (Movies, Shows, ...) - not
        # MediaFile.jellyfin_library, which is only ever set after a Jellyfin watch-status
        # sync and would leave newly-added storage locations unfilterable until then.
        query = query.join(StorageLocation, MediaFile.storage_location_id == StorageLocation.id).filter(
            StorageLocation.name == library
        )
    if q:
        query = query.filter(MediaFile.filename.ilike(f"%{q}%"))
    return query.order_by(MediaFile.genre, MediaFile.filename).all()


@app.get("/", response_class=HTMLResponse)
def root_redirect(request: Request, username: str = Depends(require_login)):
    """Redirect to system status page as the default landing page."""
    # Preserve query parameters if present (error, message, etc.)
    query_string = str(request.url.query)
    if query_string:
        return RedirectResponse(url=f"/system?{query_string}", status_code=303)
    return RedirectResponse(url="/system", status_code=303)


def _folder_view(all_movies: list[MediaFile], location: StorageLocation, folder: str) -> dict:
    """Groups all_movies (already filtered to this location) into the
    immediate children of `folder` (relative to the location's root): a
    non-movies location's content is what the hierarchical folder browser
    (docs/TODO.md) walks, one level at a time, rather than the flat list.
    Returns the leaf files at this level plus the subfolder names/counts."""
    root = Path(location.path)
    prefix_parts = tuple(p for p in folder.split("/") if p)
    subfolder_counts: dict[str, int] = {}
    leaf_files: list[MediaFile] = []
    for mf in all_movies:
        try:
            rel_parts = Path(mf.path).relative_to(root).parts
        except ValueError:
            continue
        if rel_parts[: len(prefix_parts)] != prefix_parts:
            continue
        remainder = rel_parts[len(prefix_parts) :]
        if not remainder:
            continue
        if len(remainder) == 1:
            leaf_files.append(mf)
        else:
            subfolder_counts[remainder[0]] = subfolder_counts.get(remainder[0], 0) + 1
    breadcrumbs = []
    for i, part in enumerate(prefix_parts):
        breadcrumbs.append({"name": part, "path": "/".join(prefix_parts[: i + 1])})
    return {
        "leaf_files": sorted(leaf_files, key=lambda m: m.filename.lower()),
        "subfolders": sorted(subfolder_counts.items()),
        "breadcrumbs": breadcrumbs,
    }


@app.get("/catalog", response_class=HTMLResponse)
def catalog_page(
    request: Request,
    genre: str | None = None,
    library: str | None = None,
    q: str | None = None,
    media_type: str | None = None,
    folder: str = "",
    page: int = 1,
    error: str | None = None,
    message: str | None = None,
    select_all: bool = False,
    db: Session = Depends(get_db),
    username: str = Depends(require_login),
):
    items_per_page = 20
    all_movies = list_movies(genre=genre, library=library, q=q, media_type=media_type, db=db)
    storage_locations = db.query(StorageLocation).order_by(StorageLocation.name).all()
    # Every configured local storage location (except the backup target) is a library,
    # whether or not anything's been scanned/synced into it yet.
    libraries = [
        location.name for location in storage_locations if not location.is_backup_target and location.location_type == "local"
    ]
    storage_location_by_id = {location.id: location for location in storage_locations}

    # Any non-movies library gets a hierarchical folder browser instead of a
    # flat list, once the current filters narrow the results to a single
    # library - either picked explicitly, or (e.g. a media_type=documents
    # filter with only one documents library configured) the only one left
    # standing. Folders from different libraries don't share a root to nest
    # under, so more than one candidate falls back to the flat list. Movies
    # keeps the flat genre/rating table regardless - it already has its own
    # NFO/Jellyfin-driven browsing.
    folder_location = next((loc for loc in storage_locations if loc.name == library), None) if library else None
    if folder_location is None:
        result_location_ids = {mf.storage_location_id for mf in all_movies}
        if len(result_location_ids) == 1:
            folder_location = storage_location_by_id.get(result_location_ids.pop())
    is_files_browse = folder_location is not None and folder_location.media_type != "movies"
    folder_data = None
    if is_files_browse:
        folder_data = _folder_view(all_movies, folder_location, folder)
        all_movies = folder_data["leaf_files"]

    total_items = len(all_movies)
    total_pages = (total_items + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages if total_pages > 0 else 1))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    movies = all_movies[start_idx:end_idx]
    genres = [row[0] for row in db.query(MediaFile.genre).distinct().order_by(MediaFile.genre) if row[0]]
    # Only offer types actually present in the catalog - most installs will
    # just be movies, so an empty/single-entry dropdown would be noise.
    cataloged_media_types = [
        row[0] for row in db.query(MediaFile.media_type).distinct().order_by(MediaFile.media_type) if row[0]
    ]

    # Each file backs up to its own library's archive (Libraries page).
    file_archive: dict[int, StorageLocation] = {}
    archive_by_library: dict[int, StorageLocation | None] = {}
    for mf in movies:
        if mf.storage_location_id not in archive_by_library:
            archive_by_library[mf.storage_location_id] = _archive_for_location_id(db, mf.storage_location_id)
        if archive_by_library[mf.storage_location_id]:
            file_archive[mf.id] = archive_by_library[mf.storage_location_id]

    # Where each file on this page is backed up: one entry per completed
    # BackupRecord, stacked under the file's own location in the template.
    file_locations: dict[int, list] = {}
    v2_backed: set[tuple[int, int]] = set()
    page_ids = [mf.id for mf in movies]
    if page_ids:
        records = (
            db.query(BackupRecord).filter(BackupRecord.media_file_id.in_(page_ids), BackupRecord.status == "done").all()
        )
        encrypted_by_record = dict(
            db.query(BackupRecordArchive.backup_record_id, func.bool_or(BackupArchive.encrypted))
            .join(BackupArchive, BackupArchive.id == BackupRecordArchive.backup_archive_id)
            .filter(BackupRecordArchive.backup_record_id.in_([r.id for r in records]))
            .group_by(BackupRecordArchive.backup_record_id)
            .all()
        ) if records else {}
        for record in records:
            v2_backed.add((record.media_file_id, record.destination_storage_location_id))
            file_locations.setdefault(record.media_file_id, []).append(
                SimpleNamespace(
                    destination_storage_location_id=record.destination_storage_location_id,
                    encrypted=bool(encrypted_by_record.get(record.id)),
                    compression=record.compression,
                    verify_status=record.verify_status,
                    verified_at=record.verified_at,
                )
            )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "file_archive": file_archive,
            "v2_backed": v2_backed,
            "movies": movies,
            "genres": genres,
            "cataloged_media_types": cataloged_media_types,
            "libraries": libraries,
            "storage_locations": storage_locations,
            "storage_location_by_id": storage_location_by_id,
            "file_locations": file_locations,
            "genre": genre,
            "library": library,
            "q": q,
            "media_type": media_type,
            "error": error,
            "message": message,
            "select_all": select_all,
            "active": "catalog",
            "username": username,
            "page": page,
            "total_pages": total_pages,
            "total_items": total_items,
            "is_files_browse": is_files_browse,
            "folder": folder,
            "folder_data": folder_data,
            "folder_library": folder_location.name if folder_location else library,
        },
    )


# Filesystem housekeeping folders that show up on real (especially
# NTFS-formatted external/network) drives but are never a media library.
_IGNORED_FOLDER_NAMES = {"system volume information", "$recycle.bin"}


def _list_subfolders(directory: Path) -> list[dict]:
    """Immediate subfolders of `directory` (dirs only, dotfiles and known
    filesystem-housekeeping names filtered out) - shared by the discovered-folders
    list below and the /settings/browse-folders endpoint."""
    if not directory.is_dir():
        return []
    return [
        {"name": entry.name, "path": str(entry)}
        for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower())
        if entry.is_dir() and not entry.name.startswith(".") and entry.name.lower() not in _IGNORED_FOLDER_NAMES
    ]


def _discover_media_folders(existing_paths: set[str]) -> list[dict]:
    """Immediate subfolders of the media root (e.g. /mnt/test_lib) that
    aren't already a configured storage location - lets a folder dropped
    straight onto the mount (no Jellyfin library involved) still show up as an
    addable candidate in Settings, the same way Jellyfin libraries do."""
    return [
        {**folder, "already_added": folder["path"] in existing_paths}
        for folder in _list_subfolders(Path(app_settings.media_root))
    ]


def _resolve_within_media_root(path: str | None) -> Path:
    """Resolve `path` (defaulting to the media root) and confirm it's a directory
    under the media root. `.resolve()` normalizes `..` *and* follows symlinks to
    their real target, so both traversal styles hit the same containment check.

    Callers only ever pass `path` values this endpoint itself returned in an
    earlier response (starting from the server-rendered media_root) - never
    user-typed - so resolving a relative string against the process cwd is
    never actually reached in practice."""
    root = Path(app_settings.media_root).resolve()
    resolved = Path(path).resolve() if path else root
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=403, detail="Path is outside the media root")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    return resolved


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} sec"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} hr"
    return f"{hours / 24:.1f} days"


@app.get("/settings/browse-folders", dependencies=[Depends(require_admin)])
def browse_folders(path: str | None = None):
    """Backs the Browse-to-folder dialog on the Add-storage-location form -
    lets the user click into subfolders instead of typing an exact container
    path, without exposing any filesystem access beyond the media root."""
    resolved = _resolve_within_media_root(path)
    root = Path(app_settings.media_root).resolve()
    parent = str(resolved.parent) if resolved != root else None
    return {"path": str(resolved), "parent": parent, "folders": _list_subfolders(resolved)}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    error: str | None = None,
    flash_status: str | None = None,
    flash_message: str | None = None,
    db: Session = Depends(get_db),
    username: str = Depends(require_admin),
):
    movie_count = db.query(func.count(MediaFile.id)).scalar()
    last_scanned_at = db.query(func.max(MediaFile.scanned_at)).scalar()
    db_parts = urlsplit(app_settings.database_url.replace("postgresql+psycopg", "postgresql"))
    database_host = f"{db_parts.hostname}:{db_parts.port}{db_parts.path}"

    backup_encryption_config = db.query(BackupEncryptionConfig).first()
    transfer_config = db.query(TransferConfig).first()
    notification_config = db.query(NotificationConfig).first()

    cloud_storage_config = db.query(CloudStorageConfig).first()
    tls_config = db.query(TlsConfig).first()
    all_archives = []
    for archive in db.query(StorageLocation).filter_by(is_backup_target=True).order_by(StorageLocation.location_type, StorageLocation.name):
        users = [l.name for l in db.query(StorageLocation).filter_by(archive_location_id=archive.id).all()]
        all_archives.append(
            {
                "location": archive,
                "libraries": users,
                "exists": _is_cloud_archive(archive) or Path(archive.path).is_dir(),
                "backup_count": db.query(BackupArchive).filter_by(storage_location_id=archive.id).count(),
            }
        )
    cloud_storage_buckets = (
        json.loads(cloud_storage_config.available_buckets)
        if cloud_storage_config and cloud_storage_config.available_buckets
        else []
    )

    s3_storage_config = db.query(S3StorageConfig).first()
    s3_buckets = (
        json.loads(s3_storage_config.available_buckets)
        if s3_storage_config and s3_storage_config.available_buckets
        else []
    )
    s3_effective_upload_mbps = _effective_upload_mbps(
        s3_storage_config.upload_mbps if s3_storage_config else None,
        transfer_config.max_upload_mbps if transfer_config else None,
    )

    # Rough backup-time estimate from the last measured upload speed - assumes
    # the whole library needs transferring, since nothing's actually gone to
    # cloud storage yet (no way to tell what's already "backed up" there).
    # Real uploads are capped to _effective_upload_mbps(...) - keep this in
    # sync with worker/app/gcs.py's effective_upload_mbps.
    cloud_library_total_bytes = 0
    cloud_backup_eta = None
    cloud_effective_upload_mbps = _effective_upload_mbps(
        cloud_storage_config.upload_mbps if cloud_storage_config else None,
        transfer_config.max_upload_mbps if transfer_config else None,
    )
    if cloud_effective_upload_mbps:
        cloud_library_total_bytes = int(db.query(func.sum(MediaFile.size_bytes)).scalar() or 0)
        if cloud_library_total_bytes:
            seconds = (cloud_library_total_bytes * 8 / 1_000_000) / cloud_effective_upload_mbps
            cloud_backup_eta = _format_duration(seconds)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "username": username,
            "error": error,
            "media_type_extensions": {
                media_type: ", ".join(sorted(extensions)) for media_type, extensions in MEDIA_TYPE_EXTENSIONS.items()
            },
            "database_host": database_host,
            "movie_count": movie_count,
            "last_scanned_at": last_scanned_at,
            "backup_encryption_config": backup_encryption_config,
            "key_versions": key_history(db, backup_encryption_config),
            "transfer_config": transfer_config,
            "notification_config": notification_config,
            "cloud_storage_config": cloud_storage_config,
            "all_archives": all_archives,
            "media_root": app_settings.media_root,
            "cloud_storage_buckets": cloud_storage_buckets,
            "cloud_library_total_bytes": cloud_library_total_bytes,
            "cloud_backup_eta": cloud_backup_eta,
            "cloud_effective_upload_mbps": cloud_effective_upload_mbps,
            "s3_config": s3_storage_config,
            "s3_buckets": s3_buckets,
            "s3_effective_upload_mbps": s3_effective_upload_mbps,
            "tls_config": tls_config,
            "default_upload_cap_mbps": DEFAULT_UPLOAD_CAP_MBPS,
            "flash_status": flash_status,
            "flash_message": flash_message,
        },
    )


@app.get("/integrations", response_class=HTMLResponse)
def integrations_page(
    request: Request,
    error: str | None = None,
    flash_status: str | None = None,
    flash_message: str | None = None,
    db: Session = Depends(get_db),
    username: str = Depends(require_login),
):
    """Integrations hub: Jellyfin and other external service configurations."""
    jellyfin_config = db.query(JellyfinConfig).first()
    jellyfin_users = []
    if jellyfin_config:
        try:
            jellyfin_users = jellyfin.list_users_cached(jellyfin_config.server_url, jellyfin_config.api_key)
        except httpx.HTTPError:
            pass

    watched_count = db.query(func.count(MediaFile.id)).filter(MediaFile.watched.is_(True)).scalar()

    return templates.TemplateResponse(
        request,
        "integrations.html",
        {
            "active": "integrations",
            "username": username,
            "error": error,
            "flash_status": flash_status,
            "flash_message": flash_message,
            "jellyfin_config": jellyfin_config,
            "jellyfin_users": jellyfin_users,
            "watched_count": watched_count,
        },
    )


@app.get("/libraries", response_class=HTMLResponse)
def libraries_page(
    request: Request,
    error: str | None = None,
    flash_status: str | None = None,
    flash_message: str | None = None,
    db: Session = Depends(get_db),
    username: str = Depends(require_login),
):
    everything = db.query(StorageLocation).order_by(StorageLocation.name).all()
    # An archive is a backup-target location (local folder or gs:// path); a
    # library is any other local location. Each library backs up to one archive.
    archives = [loc for loc in everything if loc.is_backup_target]
    libraries = [loc for loc in everything if loc.location_type == "local" and not loc.is_backup_target]
    # Untracked (setup.sh --fs-root "Stop tracking", or the button below) - no
    # longer scanned, shown separately so its still-restorable cloud backups
    # don't get lost from view alongside libraries actually in use.
    active_libraries = [loc for loc in libraries if not loc.untracked]
    untracked_libraries = [loc for loc in libraries if loc.untracked]
    archive_by_id = {a.id: a for a in archives}
    cloud_storage_config = db.query(CloudStorageConfig).first()
    backup_encryption_config = db.query(BackupEncryptionConfig).first()
    global_encryption_enabled = bool(backup_encryption_config and backup_encryption_config.enabled)

    library_totals: dict[int, int] = dict(
        db.query(MediaFile.storage_location_id, func.count(MediaFile.id)).group_by(MediaFile.storage_location_id).all()
    )
    media_file_library: dict[int, int] = dict(db.query(MediaFile.id, MediaFile.storage_location_id).all())

    # Files backed up to each library's *own* archive.
    backed_up: dict[int, set[int]] = {loc.id: set() for loc in libraries}
    library_archive_id = {loc.id: loc.archive_location_id for loc in libraries}
    for record in db.query(BackupRecord).filter_by(status="done"):
        library_id = media_file_library.get(record.media_file_id)
        if library_id in backed_up and record.destination_storage_location_id == library_archive_id.get(library_id):
            backed_up[library_id].add(record.media_file_id)

    library_rows = [
        {
            "location": loc,
            "archive": archive_by_id.get(loc.archive_location_id),
            "exists": Path(loc.path).is_dir(),
            "total": library_totals.get(loc.id, 0),
            "backed_up": len(backed_up[loc.id]),
            "discover_report_json": loc.discover_report_json,
        }
        for loc in active_libraries
    ]
    untracked_library_rows = [
        {
            "location": loc,
            "total": library_totals.get(loc.id, 0),
            "backed_up": len(backed_up[loc.id]),
        }
        for loc in untracked_libraries
    ]
    archive_rows = [
        {
            "location": a,
            "kind": "Cloud" if _is_cloud_archive(a) else "Local",
            "libraries": [l.name for l in libraries if l.archive_location_id == a.id],
            "exists": _is_cloud_archive(a) or Path(a.path).is_dir(),
        }
        for a in archives
    ]

    return templates.TemplateResponse(
        request,
        "libraries.html",
        {
            "active": "libraries",
            "username": username,
            "error": error,
            "flash_status": flash_status,
            "flash_message": flash_message,
            "archives": archives,
            "archive_rows": archive_rows,
            "cloud_storage_config": cloud_storage_config,
            "library_rows": library_rows,
            "untracked_library_rows": untracked_library_rows,
            "global_encryption_enabled": global_encryption_enabled,
            "media_root": app_settings.media_root,
            "media_types": MEDIA_TYPES,
            "discovered_folders": _discover_media_folders({loc.path for loc in everything}),
        },
    )


@app.post("/settings/storage-locations", dependencies=[Depends(require_admin)])
def create_storage_location(
    name: str = Form(...), path: str = Form(...), media_type: str = Form("movies"), db: Session = Depends(get_db)
):
    name = name.strip()
    path = path.strip()
    if not name or not path:
        return RedirectResponse(url="/libraries?error=Name+and+path+are+required", status_code=303)
    if media_type not in MEDIA_TYPES:
        return RedirectResponse(url="/libraries?error=Unknown+media+type", status_code=303)

    db.add(StorageLocation(name=name, path=path, location_type="local", media_type=media_type))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/libraries?error=That+path+is+already+configured", status_code=303)
    return RedirectResponse(url="/libraries", status_code=303)


def _default_archive_subpath(name: str) -> str:
    """A cloud archive folder name derived from a library's name (e.g.
    "Test Documents" -> "test_documents") - the default when a library is
    first pointed at a cloud archive with no folder typed in, so different
    libraries sharing one archive land in separate prefixes instead of mixed
    together at the root."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "library"


@app.post("/settings/storage-locations/{location_id}/update", dependencies=[Depends(require_admin)])
def update_storage_location(
    location_id: int,
    name: str = Form(...),
    path: str = Form(...),
    media_type: str = Form("movies"),
    exclude_globs: str = Form(""),
    archive_location_id: str = Form(""),
    archive_subpath: str = Form(""),
    encrypted: str = Form(""),
    db: Session = Depends(get_db),
):
    name = name.strip()
    path = path.strip()
    if not name or not path:
        return RedirectResponse(url="/libraries?error=Name+and+path+are+required", status_code=303)
    if media_type not in MEDIA_TYPES:
        return RedirectResponse(url="/libraries?error=Unknown+media+type", status_code=303)

    archive_id = int(archive_location_id) if archive_location_id.strip().isdigit() else None
    archive = db.get(StorageLocation, archive_id) if archive_id else None
    if archive_id and (archive is None or not archive.is_backup_target or archive.id == location_id):
        return RedirectResponse(url="/libraries?error=Choose+a+valid+archive", status_code=303)
    try:
        subpath = normalize_prefix(archive_subpath).rstrip("/")
    except ValueError as exc:
        return RedirectResponse(url=f"/libraries?error={quote(str(exc))}", status_code=303)
    if subpath and not _is_cloud_archive(archive):
        return RedirectResponse(
            url="/libraries?error=Archive+subfolders+are+only+supported+on+cloud+archives+for+now", status_code=303
        )

    location = db.get(StorageLocation, location_id)
    if location:
        old_path = location.path
        newly_assigned = archive is not None and location.archive_location_id != archive.id
        if newly_assigned:
            # Repointing to a different archive invalidates whatever was
            # cached against the old one - re-checked below once the new
            # archive/subpath is committed.
            location.discover_report_json = None
        location.archive_location_id = archive.id if archive else None
        if not subpath and newly_assigned and _is_cloud_archive(archive):
            # First time this library is pointed at a cloud archive and no
            # folder was typed - default to one named after the library, so
            # different libraries sharing one archive don't land in the same
            # prefix. Only on first assignment: an explicit blank on a later
            # edit (same archive, subpath cleared on purpose) is left alone.
            subpath = _default_archive_subpath(name)
        location.archive_subpath = subpath or None
        # Tri-state: "" -> None (inherit the global Backup Encryption
        # setting), "true"/"false" -> explicit per-library override.
        location.encrypted = {"true": True, "false": False}.get(encrypted)
        location.name = name
        location.path = path
        location.media_type = media_type
        location.exclude_globs = "\n".join(parse_exclude_globs(exclude_globs)) or None
        if old_path != path:
            # MediaFile.path is looked up by exact string match on scan
            # (catalog.py:_scan_location) - left stale, every one of this
            # library's already-cataloged files would be re-added as a
            # duplicate row on the next scan, and the old row (carrying any
            # BackupRecord history) would be flagged missing, orphaning the
            # backup from the file that's actually still there. Rewriting the
            # prefix in place keeps each MediaFile's id, and with it any
            # backup history, attached to the same file.
            db.execute(
                text(
                    "UPDATE media_files SET path = :new_prefix || substring(path from :old_len + 1) "
                    "WHERE storage_location_id = :loc_id AND starts_with(path, :old_prefix)"
                ),
                {"new_prefix": path, "old_len": len(old_path), "loc_id": location.id, "old_prefix": old_path},
            )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return RedirectResponse(url="/libraries?error=That+path+is+already+configured", status_code=303)

        # A folder typed in that doesn't exist in the bucket yet is created now
        # (a zero-byte placeholder), so it shows up when browsing the archive. A
        # failure here isn't fatal: the folder is also created by the first
        # backup that writes into it.
        if archive and _is_cloud_archive(archive) and subpath:
            try:
                if archive.location_type == "gcs":
                    config = _get_cloud_storage_config(db)
                    if config and config.service_account_json:
                        bucket, base = parse_gcs_path(archive.path)
                        gcs.create_folder(config.service_account_json, bucket, normalize_prefix(base, subpath), config.project_id)
                else:
                    s3_config = _get_s3_storage_config(db)
                    if s3_config and s3_config.access_key_id:
                        bucket, base = parse_s3_path(archive.path)
                        s3.create_folder(s3_config.access_key_id, s3_config.secret_access_key, bucket, normalize_prefix(base, subpath), s3_config.region)
            except Exception as exc:
                note = f"Saved, but could not create the folder in the bucket yet ({str(exc)[:100]}). It will be created on first backup."
                return RedirectResponse(url=f"/libraries?flash_status=error&flash_message={quote(note)}", status_code=303)

        # A library just pointed at a cloud archive for the first time may
        # already have content there from another instance (or a reset
        # database) - the portability scenario. Check now, proactively,
        # instead of waiting for the user to know to click "Check bucket"
        # (docs/backup-plan/steps/18-portability.md). Best-effort: a failed
        # check here just leaves the cache empty, same as before this
        # library was ever checked. GCS-only for now (_discover_bucket_report).
        if newly_assigned and archive.location_type == "gcs":
            try:
                report = _discover_bucket_report(db, location, archive)
                if "error" not in report:
                    location.discover_report_json = json.dumps(report)
                    db.commit()
            except Exception:
                logging.getLogger(__name__).exception("auto discover-bucket check failed for library %s", location.id)
    return RedirectResponse(url="/libraries", status_code=303)


@app.get("/settings/cloud-storage/browse", dependencies=[Depends(require_admin)])
def browse_cloud_bucket(bucket: str, path: str = "", db: Session = Depends(get_db)):
    """Lists folders inside a bucket, for picking an archive's directory before
    the archive exists (the archive-level browser is /libraries/browse-archive)."""
    config = _get_cloud_storage_config(db)
    if not config or not config.service_account_json:
        raise HTTPException(status_code=400, detail="No Google Cloud service account is configured")
    try:
        bucket_name, _ = parse_gcs_path(f"gs://{bucket.strip()}")
        rel = normalize_prefix(path)
        folders = gcs.list_prefixes(config.service_account_json, bucket_name, rel, config.project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not list bucket: {str(exc)[:160]}")
    return {"bucket": bucket_name, "path": rel.rstrip("/"), "folders": folders}


@app.post("/settings/cloud-storage/archives", dependencies=[Depends(require_admin)])
def create_cloud_archive(
    name: str = Form(...), path: str = Form(...), storage_class: str = Form(""), db: Session = Depends(get_db)
):
    """Adds a Google Cloud archive: a gs://bucket/folder path libraries can back
    up to. All cloud archives share the one service account under Cloud Storage."""
    name = name.strip()
    config = _get_cloud_storage_config(db)
    if not config or not config.service_account_json:
        return RedirectResponse(url="/settings?error=Add+a+Google+Cloud+service+account+key+first#cloud-storage", status_code=303)
    try:
        bucket, prefix = parse_gcs_path(path)
    except ValueError as exc:
        return RedirectResponse(url=f"/settings?error={quote(str(exc))}#cloud-storage", status_code=303)
    if not name:
        name = format_gcs_path(bucket, prefix)
    storage_class = storage_class.strip().upper()
    if storage_class and storage_class not in gcs.STORAGE_CLASSES:
        return RedirectResponse(url="/settings?error=Unknown+storage+class#cloud-storage", status_code=303)
    try:
        gcs.verify_bucket_access(config.service_account_json, bucket, config.project_id)
    except Exception as exc:
        return RedirectResponse(url=f"/settings?error={quote('Cannot access bucket ' + bucket + ': ' + str(exc)[:160])}#cloud-storage", status_code=303)

    db.add(
        StorageLocation(
            name=name,
            path=format_gcs_path(bucket, prefix),
            location_type="gcs",
            media_type="files",
            is_backup_target=True,
            storage_class=storage_class or None,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/settings?error=That+archive+path+already+exists#cloud-storage", status_code=303)
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Cloud+archive+added#archives", status_code=303)


@app.post("/archives/{archive_id}/delete", dependencies=[Depends(require_login)])
def delete_archive(archive_id: int, next: str = Form("/libraries"), force: bool = Form(False), db: Session = Depends(get_db)):
    """Removes an archive definition. Always refused while a library still points
    at it. If it holds recorded backups, refused unless `force` is set - forcing
    deletes those backup records too (the bucket's own objects are untouched, but
    the app loses track of them and they're no longer restorable from here)."""
    back = next if next in ("/libraries", "/settings#archives", "/settings#cloud-storage", "/settings#aws-provider") else "/libraries"
    archive = db.get(StorageLocation, archive_id)
    if not archive or not archive.is_backup_target:
        return RedirectResponse(url=f"{back}", status_code=303)
    in_use = db.query(StorageLocation).filter_by(archive_location_id=archive_id).count()
    if in_use:
        return RedirectResponse(url=f"/libraries?error={quote(f'{in_use} library(ies) still use this archive - reassign them first')}", status_code=303)
    backup_count = db.query(BackupArchive).filter_by(storage_location_id=archive_id).count()
    if backup_count and not force:
        return RedirectResponse(
            url=f"/libraries?error={quote(f'This archive holds {backup_count} recorded backup archive(s); use Force Remove to delete it anyway')}",
            status_code=303,
        )
    db.delete(archive)
    db.commit()
    return RedirectResponse(url=back, status_code=303)


@app.get("/libraries/browse-archive", dependencies=[Depends(require_login)])
def browse_archive(archive_id: int, path: str = "", db: Session = Depends(get_db)):
    """Lists subfolders inside a cloud archive so a library can pick one. `path`
    is relative to the archive's own gs://bucket/folder."""
    archive = db.get(StorageLocation, archive_id)
    if not _is_cloud_archive(archive):
        raise HTTPException(status_code=400, detail="Only cloud archives can be browsed")
    try:
        rel = normalize_prefix(path)
        if archive.location_type == "gcs":
            config = _get_cloud_storage_config(db)
            if not config or not config.service_account_json:
                raise HTTPException(status_code=400, detail="No Google Cloud service account is configured")
            bucket, base = parse_gcs_path(archive.path)
            folders = gcs.list_prefixes(config.service_account_json, bucket, normalize_prefix(base, rel), config.project_id)
        else:
            s3_config = _get_s3_storage_config(db)
            if not s3_config or not s3_config.access_key_id:
                raise HTTPException(status_code=400, detail="No AWS credentials are configured")
            bucket, base = parse_s3_path(archive.path)
            folders = s3.list_prefixes(s3_config.access_key_id, s3_config.secret_access_key, bucket, normalize_prefix(base, rel), s3_config.region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not list bucket: {str(exc)[:160]}")
    return {"archive": archive.path, "path": rel.rstrip("/"), "folders": folders}


@app.post("/libraries/archive-folders", dependencies=[Depends(require_login)])
def create_archive_folder(
    archive_id: int = Form(...), path: str = Form(""), name: str = Form(...), db: Session = Depends(get_db)
):
    """Creates a folder inside a cloud archive (a zero-byte placeholder object),
    so a library can be pointed at a folder that doesn't exist yet. `path` is
    the parent, relative to the archive's own gs://bucket/folder."""
    archive = db.get(StorageLocation, archive_id)
    if not _is_cloud_archive(archive):
        raise HTTPException(status_code=400, detail="Folders can only be created in cloud archives")
    leaf = name.strip().strip("/")
    if not leaf or "/" in leaf:
        raise HTTPException(status_code=400, detail="Folder name must be a single name without '/'")
    try:
        rel = normalize_prefix(path, leaf)
        if archive.location_type == "gcs":
            config = _get_cloud_storage_config(db)
            if not config or not config.service_account_json:
                raise HTTPException(status_code=400, detail="No Google Cloud service account is configured")
            bucket, base = parse_gcs_path(archive.path)
            gcs.create_folder(config.service_account_json, bucket, normalize_prefix(base, rel), config.project_id)
        else:
            s3_config = _get_s3_storage_config(db)
            if not s3_config or not s3_config.access_key_id:
                raise HTTPException(status_code=400, detail="No AWS credentials are configured")
            bucket, base = parse_s3_path(archive.path)
            s3.create_folder(s3_config.access_key_id, s3_config.secret_access_key, bucket, normalize_prefix(base, rel), s3_config.region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not create folder: {str(exc)[:160]}")
    return {"path": rel.rstrip("/")}


@app.post("/libraries/{location_id}/backup", dependencies=[Depends(require_login)])
def backup_library(location_id: int, mode: str = Form("replace_older"), db: Session = Depends(get_db)):
    """Backs the whole library up to its assigned archive as a tracked run."""
    library = db.get(StorageLocation, location_id)
    archive = _archive_for_location_id(db, location_id)
    if not library:
        return RedirectResponse(url="/libraries?error=Library+not+found", status_code=303)
    if not archive:
        return RedirectResponse(url=f"/libraries?error={quote(library.name + ' has no archive assigned')}", status_code=303)
    runs = _start_batched_backup_runs(db, library.id, archive, "library", mode=_validate_run_mode(mode))
    if not runs:
        return RedirectResponse(url=f"/libraries?error={quote(library.name + ' has no files to back up')}", status_code=303)
    message = f"Started backup of {library.name} to {archive.name}"
    if len(runs) > 1:
        message += f" as {len(runs)} batches (keeps disk use bounded on a large library)"
    message += ". Track it under Backup Runs."
    return RedirectResponse(url=f"/copy-jobs?message={quote(message)}", status_code=303)


@app.post("/settings/storage-locations/{location_id}/delete", dependencies=[Depends(require_admin)])
def delete_storage_location(location_id: int, db: Session = Depends(get_db)):
    location = db.get(StorageLocation, location_id)
    if location:
        db.query(MediaFile).filter_by(storage_location_id=location.id).delete()
        db.delete(location)
        db.commit()
    return RedirectResponse(url="/libraries", status_code=303)


@app.post("/settings/storage-locations/{location_id}/untrack", dependencies=[Depends(require_admin)])
def untrack_storage_location(location_id: int, db: Session = Depends(get_db)):
    """Stops tracking a library without deleting anything - unlike delete_storage_location
    above, this keeps the StorageLocation, its MediaFile rows, and (crucially) their
    BackupRecord/BackupRecordArchive rows intact, so cloud backups stay restorable from
    the Catalog page. Excluded from catalog.scan_library from now on; existing files are
    marked missing immediately rather than waiting for a scan that will never come."""
    location = db.get(StorageLocation, location_id)
    if not location:
        return RedirectResponse(url="/libraries?error=Library+not+found", status_code=303)
    location.untracked = True
    db.query(MediaFile).filter_by(storage_location_id=location.id).update({"is_missing": True})
    db.commit()
    message = quote(f"Stopped tracking {location.name}. Its cloud backups are still available to restore.")
    return RedirectResponse(url=f"/libraries?flash_status=ok&flash_message={message}", status_code=303)


@app.post("/settings/storage-locations/{location_id}/retrack", dependencies=[Depends(require_admin)])
def retrack_storage_location(location_id: int, db: Session = Depends(get_db)):
    location = db.get(StorageLocation, location_id)
    if not location:
        return RedirectResponse(url="/libraries?error=Library+not+found", status_code=303)
    location.untracked = False
    db.commit()
    message = quote(f"Resumed tracking {location.name}. Rescan once its path is correct.")
    return RedirectResponse(url=f"/libraries?flash_status=ok&flash_message={message}", status_code=303)


RUN_MODES = ("replace_older", "replace_all")


def _validate_run_mode(mode: str | None) -> str:
    """Allowlists the run mode reaching the DB - anything else (missing,
    unrecognized) falls back to the safe default rather than being stored."""
    return mode if mode in RUN_MODES else "replace_older"


def _start_backup_run(
    db: Session,
    source_id: int,
    destination: StorageLocation,
    scope: str,
    file_ids: list[int] | None = None,
    mode: str = "replace_older",
) -> BackupRun:
    """Creates the tracking row (so the run shows on Copy Jobs immediately, as
    "queued") and hands it to the worker's v2 run_backup. file_ids None means
    the whole source library. mode is "replace_older" (skip-unchanged, the
    default) or "replace_all" (force re-upload every file)."""
    run = BackupRun(
        scope=scope,
        source_storage_location_id=source_id,
        destination_storage_location_id=destination.id,
        file_ids=json.dumps(file_ids) if file_ids is not None else None,
        mode=_validate_run_mode(mode),
        status="queued",
    )
    db.add(run)
    db.commit()
    run.celery_task_id = enqueue_run_backup(source_id, destination.id, file_ids, run.id)
    db.commit()
    return run


DEFAULT_BATCH_BYTES = 5 * 1024**3  # 5 GiB, used until TransferConfig exists


def _get_batch_bytes(db: Session) -> int:
    config = db.query(TransferConfig).first()
    return config.batch_bytes if config and config.batch_bytes else DEFAULT_BATCH_BYTES


def _batch_media_file_ids(db: Session, source_id: int, file_ids: list[int] | None, batch_bytes: int) -> list[list[int]]:
    """Splits a library's files (or an explicit selection) into groups bounded
    by total size, so no single BackupRun asks the worker to hold more than
    ~batch_bytes of compressed/encrypted data on local disk at once (see
    worker/app/backup_run.py's _prepare_candidates, which writes every file in
    a run to disk before packing starts). Doesn't split a single file that's
    larger than batch_bytes on its own - that file just becomes its own batch."""
    query = db.query(MediaFile.id, MediaFile.size_bytes).filter_by(storage_location_id=source_id)
    if file_ids is not None:
        query = query.filter(MediaFile.id.in_(file_ids))
    batches: list[list[int]] = []
    current: list[int] = []
    current_bytes = 0
    for mf_id, size_bytes in query.order_by(MediaFile.id):
        size_bytes = size_bytes or 0
        if current and current_bytes + size_bytes > batch_bytes:
            batches.append(current)
            current, current_bytes = [], 0
        current.append(mf_id)
        current_bytes += size_bytes
    if current:
        batches.append(current)
    return batches


def _start_batched_backup_runs(
    db: Session,
    source_id: int,
    destination: StorageLocation,
    scope: str,
    file_ids: list[int] | None = None,
    mode: str = "replace_older",
) -> list[BackupRun]:
    """Like _start_backup_run, but splits large runs into several batches
    (see _batch_media_file_ids) instead of one - run_backup is serialized one
    at a time (a Postgres advisory lock), so batches never overlap on disk,
    bounding peak disk use to about one batch instead of the whole run."""
    batches = _batch_media_file_ids(db, source_id, file_ids, _get_batch_bytes(db))
    if not batches:
        return []
    if len(batches) == 1 and file_ids is None:
        # Everything fits in one batch anyway - keep this identical to the
        # old whole-library behavior (file_ids=None) rather than listing
        # every id in the run for no reason.
        return [_start_backup_run(db, source_id, destination, scope, None, mode=mode)]
    return [_start_backup_run(db, source_id, destination, scope, batch, mode=mode) for batch in batches]


@app.post("/settings/storage-locations/{location_id}/backup-cloud", dependencies=[Depends(require_admin)])
def backup_library_to_cloud(location_id: int, mode: str = Form("replace_older"), db: Session = Depends(get_db)):
    library = db.get(StorageLocation, location_id)
    cloud_backup_location = _get_cloud_backup_location(db)
    if not library:
        return RedirectResponse(url="/libraries?error=Library+not+found", status_code=303)
    if not cloud_backup_location:
        return RedirectResponse(url="/libraries?error=No+cloud+archive+configured", status_code=303)

    # v2 pipeline (docs/cfa-spec.md, DEVIATIONS.md D8/D9): one whole-library run,
    # encrypted per Settings, skipping files unchanged since their last backup.
    _start_backup_run(db, library.id, cloud_backup_location, "library", mode=_validate_run_mode(mode))
    message = f"Started cloud backup of {library.name}. Track it under Backup Runs."
    return RedirectResponse(url=f"/copy-jobs?message={quote(message)}", status_code=303)


@app.post("/settings/backup-encryption", dependencies=[Depends(require_admin)])
def save_backup_encryption(
    enabled: bool = Form(False), password: str = Form(""), salt: str = Form(""), db: Session = Depends(get_db)
):
    config = db.query(BackupEncryptionConfig).first()
    if not config:
        config = BackupEncryptionConfig()
        db.add(config)

    password = password.strip()
    salt = salt.strip().lower()
    if salt and not kdf_salt_is_valid_hex(salt):
        return RedirectResponse(
            url="/settings?error=Salt+must+be+exactly+32+hex+characters+-+copy+it+from+the+other+instance's+Settings+page",
            status_code=303,
        )
    if password:
        # A new password (with no salt given) gets a new salt, i.e. a new
        # key - the previous key is kept in Key History, and each v2 archive
        # records which key encrypted it, so existing backups stay
        # restorable. Giving the *old* instance's salt here instead
        # reproduces that instance's exact key, so this one can decrypt (and
        # discover) its backups too - see /libraries/{id}/discover-bucket.
        # Same password+salt again = no change.
        set_passphrase(db, config, password, salt=salt or None)
    elif salt:
        return RedirectResponse(url="/settings?error=Enter+the+password+that+goes+with+that+salt", status_code=303)
    elif enabled and not config.password:
        return RedirectResponse(url="/settings?error=Set+a+password+before+enabling+encryption", status_code=303)

    config.enabled = enabled
    db.commit()
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Backup+encryption+settings+saved", status_code=303)


@app.post("/settings/compression", dependencies=[Depends(require_admin)])
def save_compression_config(
    compression_enabled: str = Form(""),
    compression_level: int = Form(6),
    db: Session = Depends(get_db),
):
    if not 1 <= compression_level <= 9:
        return RedirectResponse(url="/settings?error=Compression+level+must+be+between+1+and+9", status_code=303)

    config = db.query(TransferConfig).first()
    if not config:
        config = TransferConfig()
        db.add(config)

    config.compression_enabled = bool(compression_enabled)
    config.compression_level = compression_level
    db.commit()
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Compression+settings+saved", status_code=303)


@app.post("/settings/bandwidth", dependencies=[Depends(require_admin)])
def save_bandwidth_config(max_upload_mbps: str = Form(""), db: Session = Depends(get_db)):
    """A hard ceiling on upload speed (Mbps), enforced in worker/app/gcs.py -
    see _effective_upload_mbps above."""
    cloud_storage_config = db.query(CloudStorageConfig).first()
    value, error = parse_upload_limit(
        max_upload_mbps, cloud_storage_config.upload_mbps if cloud_storage_config else None
    )
    if error:
        return RedirectResponse(url=f"/settings?error={error}", status_code=303)

    config = db.query(TransferConfig).first()
    if not config:
        config = TransferConfig()
        db.add(config)

    config.max_upload_mbps = value
    db.commit()
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Bandwidth+settings+saved", status_code=303)


@app.post("/settings/backup-archiving", dependencies=[Depends(require_admin)])
def save_backup_archiving_config(
    min_size_mib: float = Form(...),
    max_size_mib: float = Form(...),
    batch_gib: float = Form(5.0),
    prefix: str = Form(""),
    db: Session = Depends(get_db),
):
    # Form fields are MiB (GiB for batch_gib) for legibility; stored as bytes
    # (see app/models.py:TransferConfig) so step 3's part-count math is exact.
    min_size_bytes = int(round(min_size_mib * MIB))
    max_size_bytes = int(round(max_size_mib * MIB))
    clump_size_bytes = 64 * MIB  # Fixed at 64 MiB per spec

    error = validate_archive_sizes(min_size_bytes, clump_size_bytes, max_size_bytes)
    if error:
        return RedirectResponse(url=f"/settings?error={error}", status_code=303)
    if batch_gib < 0.5:
        return RedirectResponse(url="/settings?error=Batch+size+must+be+at+least+0.5+GiB", status_code=303)
    batch_bytes = int(round(batch_gib * 1024**3))

    transfer_config = db.query(TransferConfig).first()
    if not transfer_config:
        transfer_config = TransferConfig()
        db.add(transfer_config)
    transfer_config.min_size_bytes = min_size_bytes
    transfer_config.clump_size_bytes = clump_size_bytes
    transfer_config.max_size_bytes = max_size_bytes
    transfer_config.batch_bytes = batch_bytes

    cloud_storage_config = db.query(CloudStorageConfig).first()
    if not cloud_storage_config:
        cloud_storage_config = CloudStorageConfig()
        db.add(cloud_storage_config)
    cloud_storage_config.prefix = normalize_prefix(prefix)

    db.commit()
    return RedirectResponse(
        url="/settings?flash_status=ok&flash_message=Transfer+settings+saved", status_code=303
    )


@app.post("/settings/notifications", dependencies=[Depends(require_admin)])
def save_notification_config(
    browser_enabled: bool = Form(False),
    email_enabled: bool = Form(False),
    smtp_host: str = Form(""),
    smtp_port: int = Form(465),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_to: str = Form(""),
    pushover_enabled: bool = Form(False),
    pushover_api_token: str = Form(""),
    pushover_user_key: str = Form(""),
    notify_backup_done: bool = Form(False),
    notify_backup_failed: bool = Form(False),
    notify_restore_done: bool = Form(False),
    notify_restore_failed: bool = Form(False),
    db: Session = Depends(get_db),
):
    config = db.query(NotificationConfig).first()
    if not config:
        config = NotificationConfig()
        db.add(config)
    config.browser_enabled = browser_enabled
    config.email_enabled = email_enabled
    config.smtp_host = smtp_host.strip() or None
    config.smtp_port = smtp_port
    config.smtp_username = smtp_username.strip() or None
    # A blank password field means "leave it as-is" (an existing password
    # is never rendered back into the form), not "clear the password" -
    # matches the pattern already used for the cloud service account key.
    if smtp_password.strip():
        config.smtp_password = smtp_password.strip()
    config.smtp_from = smtp_from.strip() or None
    config.smtp_to = smtp_to.strip() or None
    config.pushover_enabled = pushover_enabled
    config.pushover_api_token = pushover_api_token.strip() or None
    config.pushover_user_key = pushover_user_key.strip() or None
    config.notify_backup_done = notify_backup_done
    config.notify_backup_failed = notify_backup_failed
    config.notify_restore_done = notify_restore_done
    config.notify_restore_failed = notify_restore_failed
    db.commit()
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Notification+settings+saved#notifications", status_code=303)


def _send_test_email(config: NotificationConfig) -> str | None:
    """Mirrors worker/app/notify.py's _send_email - duplicated rather than
    shared, matching this app's existing web/worker split (see CLAUDE.md).
    Returns an error string, or None on success."""
    if not (config.smtp_host and config.smtp_from and config.smtp_to):
        return "SMTP host, from address, and to address are all required"
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from smtplib import SMTP, SMTP_SSL

    msg = MIMEMultipart()
    msg.attach(MIMEText("This is a test notification from Diplio's Settings > Notifications page.", "plain"))
    msg["Subject"] = "Diplio test notification"
    msg["From"] = config.smtp_from
    msg["To"] = config.smtp_to
    conn = None
    try:
        if config.smtp_password:
            try:
                conn = SMTP_SSL(config.smtp_host, config.smtp_port, timeout=10)
                conn.login(config.smtp_username or config.smtp_from, config.smtp_password)
            except Exception:
                conn = SMTP(config.smtp_host, config.smtp_port, timeout=10)
                conn.login(config.smtp_username or config.smtp_from, config.smtp_password)
        else:
            conn = SMTP(config.smtp_host, config.smtp_port, timeout=10)
        conn.sendmail(config.smtp_from, config.smtp_to.split(","), msg.as_string())
        return None
    except Exception as exc:
        return str(exc)[:300]
    finally:
        if conn:
            try:
                conn.quit()
            except Exception:
                pass


@app.post("/settings/notifications/test-email", dependencies=[Depends(require_admin)])
def test_notification_email(db: Session = Depends(get_db)):
    config = db.query(NotificationConfig).first()
    if not config:
        return RedirectResponse(url="/settings?error=Save+notification+settings+first#notifications", status_code=303)
    error = _send_test_email(config)
    if error:
        return RedirectResponse(url=f"/settings?error={quote('Test email failed: ' + error)}#notifications", status_code=303)
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Test+email+sent#notifications", status_code=303)


@app.post("/settings/notifications/test-pushover", dependencies=[Depends(require_admin)])
def test_notification_pushover(db: Session = Depends(get_db)):
    config = db.query(NotificationConfig).first()
    if not config or not (config.pushover_api_token and config.pushover_user_key):
        return RedirectResponse(url="/settings?error=Pushover+token+and+user+key+are+both+required#notifications", status_code=303)
    try:
        resp = httpx.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": config.pushover_api_token,
                "user": config.pushover_user_key,
                "title": "Diplio test notification",
                "message": "This is a test notification from Diplio's Settings > Notifications page.",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return RedirectResponse(
                url=f"/settings?error={quote('Pushover rejected the request: ' + resp.text[:200])}#notifications", status_code=303
            )
    except Exception as exc:
        return RedirectResponse(url=f"/settings?error={quote('Test pushover failed: ' + str(exc)[:200])}#notifications", status_code=303)
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Test+pushover+notification+sent#notifications", status_code=303)


@app.get("/notifications/poll", dependencies=[Depends(require_login)])
def poll_notifications(after_id: int = 0, db: Session = Depends(get_db)) -> dict:
    """Polled by base.html's site-wide script (not just Copy Jobs) to surface
    browser notifications for anything worth knowing about, regardless of
    which page is open. Returns nothing if browser notifications are off."""
    config = db.query(NotificationConfig).first()
    if not config or not config.browser_enabled:
        return {"events": [], "latest_id": after_id}
    events = (
        db.query(NotificationEvent)
        .filter(NotificationEvent.id > after_id)
        .order_by(NotificationEvent.id)
        .limit(20)
        .all()
    )
    latest_id = events[-1].id if events else after_id
    return {
        "events": [{"id": e.id, "level": e.level, "title": e.title, "message": e.message} for e in events],
        "latest_id": latest_id,
    }


def _get_cloud_storage_config(db: Session) -> CloudStorageConfig | None:
    return db.query(CloudStorageConfig).first()


def _get_s3_storage_config(db: Session) -> S3StorageConfig | None:
    return db.query(S3StorageConfig).first()


def _is_cloud_archive(loc: StorageLocation | None) -> bool:
    """True for any cloud-backed archive (GCS or S3), as opposed to a local
    folder. Use this instead of a bare `location_type == "gcs"` check
    anywhere the distinction is "cloud vs local", not "which provider"."""
    return bool(loc) and loc.location_type in ("gcs", "s3")


@app.post("/settings/cloud-storage/credentials", dependencies=[Depends(require_admin)])
async def save_cloud_storage_credentials(key_file: UploadFile = File(...), db: Session = Depends(get_db)):
    raw = (await key_file.read()).decode("utf-8", errors="replace")
    try:
        data = gcs.parse_and_validate_key(raw)
    except ValueError as exc:
        return RedirectResponse(url=f"/settings?error={quote(str(exc))}", status_code=303)

    config = _get_cloud_storage_config(db)
    if not config:
        config = CloudStorageConfig()
        db.add(config)

    # A new key invalidates any bucket list/selection fetched with the old one.
    config.service_account_json = raw
    config.service_account_email = data["client_email"]
    config.project_id = data["project_id"]
    config.available_buckets = None
    config.bucket_name = None
    config.connected = False
    config.last_error = None
    db.commit()
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Service+account+key+saved", status_code=303)


@app.post("/settings/cloud-storage/list-buckets", dependencies=[Depends(require_admin)])
def list_cloud_storage_buckets(db: Session = Depends(get_db)):
    config = _get_cloud_storage_config(db)
    if not config or not config.service_account_json:
        return RedirectResponse(url="/settings?error=Upload+a+service+account+key+first", status_code=303)

    try:
        buckets = gcs.list_buckets(config.service_account_json, config.project_id)
    except Exception as exc:
        config.last_error = str(exc)[:1000]
        db.commit()
        return RedirectResponse(url=f"/settings?error={quote('Could not list buckets: ' + str(exc)[:200])}", status_code=303)

    config.available_buckets = json.dumps(buckets)
    config.last_error = None
    db.commit()
    message = f"Found {len(buckets)} bucket(s)" if buckets else "No buckets found in this project"
    return RedirectResponse(url=f"/settings?flash_status=ok&flash_message={quote(message)}", status_code=303)


@app.post("/settings/cloud-storage/bucket", dependencies=[Depends(require_admin)])
def select_cloud_storage_bucket(bucket_name: str = Form(...), db: Session = Depends(get_db)):
    config = _get_cloud_storage_config(db)
    if not config or not config.service_account_json:
        return RedirectResponse(url="/settings?error=Connect+a+service+account+first", status_code=303)
    bucket_name = bucket_name.strip()
    if not bucket_name:
        return RedirectResponse(url="/settings?error=Bucket+name+cannot+be+blank", status_code=303)

    try:
        gcs.verify_bucket_access(config.service_account_json, bucket_name, config.project_id)
    except Exception as exc:
        config.connected = False
        config.last_error = str(exc)[:1000]
        db.commit()
        return RedirectResponse(url=f"/settings?error={quote(str(exc)[:200])}", status_code=303)

    config.bucket_name = bucket_name
    config.connected = True
    config.last_error = None
    db.commit()
    return RedirectResponse(url=f"/settings?flash_status=ok&flash_message=Connected+to+bucket+{quote(bucket_name)}", status_code=303)


@app.post("/settings/cloud-storage/test-speed", dependencies=[Depends(require_admin)])
def test_cloud_storage_speed(db: Session = Depends(get_db)):
    config = _get_cloud_storage_config(db)
    if not config or not config.service_account_json or not config.bucket_name:
        return RedirectResponse(url="/settings?error=Connect+to+a+bucket+first", status_code=303)

    try:
        result = gcs.test_connectivity_and_speed(config.service_account_json, config.bucket_name, config.project_id)
    except Exception as exc:
        config.last_error = str(exc)[:1000]
        db.commit()
        return RedirectResponse(url=f"/settings?error={quote('Connectivity test failed: ' + str(exc)[:200])}", status_code=303)

    config.upload_mbps = result["upload_mbps"]
    config.download_mbps = result["download_mbps"]
    config.last_speed_test_at = datetime.now(timezone.utc)
    config.last_error = None
    db.commit()
    message = f"Connectivity OK - {result['upload_mbps']:.1f} Mbps up / {result['download_mbps']:.1f} Mbps down"
    return RedirectResponse(url=f"/settings?flash_status=ok&flash_message={quote(message)}", status_code=303)


@app.post("/settings/cloud-storage/disconnect", dependencies=[Depends(require_admin)])
def disconnect_cloud_storage(db: Session = Depends(get_db)):
    config = _get_cloud_storage_config(db)
    if config:
        db.delete(config)
        db.commit()
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Cloud+storage+disconnected", status_code=303)


@app.post("/settings/s3-storage/credentials", dependencies=[Depends(require_admin)])
def save_s3_storage_credentials(
    access_key_id: str = Form(...), secret_access_key: str = Form(...), region: str = Form(""), db: Session = Depends(get_db)
):
    access_key_id = access_key_id.strip()
    secret_access_key = secret_access_key.strip()
    region = region.strip() or None
    if not access_key_id or not secret_access_key:
        return RedirectResponse(url="/settings?error=Access+key+id+and+secret+access+key+are+required#aws-provider", status_code=303)
    try:
        s3.verify_credentials(access_key_id, secret_access_key, region)
    except ValueError as exc:
        return RedirectResponse(url=f"/settings?error={quote(str(exc)[:200])}#aws-provider", status_code=303)

    config = _get_s3_storage_config(db)
    if not config:
        config = S3StorageConfig()
        db.add(config)

    # New credentials invalidate any bucket list/selection fetched with the old ones.
    config.access_key_id = access_key_id
    config.secret_access_key = secret_access_key
    config.region = region
    config.available_buckets = None
    config.bucket_name = None
    config.connected = False
    config.last_error = None
    db.commit()
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=AWS+credentials+saved#aws-provider", status_code=303)


@app.post("/settings/s3-storage/list-buckets", dependencies=[Depends(require_admin)])
def list_s3_storage_buckets(db: Session = Depends(get_db)):
    config = _get_s3_storage_config(db)
    if not config or not config.access_key_id:
        return RedirectResponse(url="/settings?error=Add+AWS+credentials+first#aws-provider", status_code=303)

    try:
        buckets = s3.list_buckets(config.access_key_id, config.secret_access_key, config.region)
    except Exception as exc:
        config.last_error = str(exc)[:1000]
        db.commit()
        return RedirectResponse(url=f"/settings?error={quote('Could not list buckets: ' + str(exc)[:200])}#aws-provider", status_code=303)

    config.available_buckets = json.dumps(buckets)
    config.last_error = None
    db.commit()
    message = f"Found {len(buckets)} bucket(s)" if buckets else "No buckets found"
    return RedirectResponse(url=f"/settings?flash_status=ok&flash_message={quote(message)}#aws-provider", status_code=303)


@app.post("/settings/s3-storage/bucket", dependencies=[Depends(require_admin)])
def select_s3_storage_bucket(bucket_name: str = Form(...), db: Session = Depends(get_db)):
    config = _get_s3_storage_config(db)
    if not config or not config.access_key_id:
        return RedirectResponse(url="/settings?error=Add+AWS+credentials+first#aws-provider", status_code=303)
    bucket_name = bucket_name.strip()
    if not bucket_name:
        return RedirectResponse(url="/settings?error=Bucket+name+cannot+be+blank#aws-provider", status_code=303)

    try:
        s3.verify_bucket_access(config.access_key_id, config.secret_access_key, bucket_name, config.region)
    except Exception as exc:
        config.connected = False
        config.last_error = str(exc)[:1000]
        db.commit()
        return RedirectResponse(url=f"/settings?error={quote(str(exc)[:200])}#aws-provider", status_code=303)

    config.bucket_name = bucket_name
    config.connected = True
    config.last_error = None
    db.commit()
    return RedirectResponse(url=f"/settings?flash_status=ok&flash_message=Connected+to+bucket+{quote(bucket_name)}#aws-provider", status_code=303)


@app.post("/settings/s3-storage/test-speed", dependencies=[Depends(require_admin)])
def test_s3_storage_speed(db: Session = Depends(get_db)):
    config = _get_s3_storage_config(db)
    if not config or not config.access_key_id or not config.bucket_name:
        return RedirectResponse(url="/settings?error=Connect+to+a+bucket+first#aws-provider", status_code=303)

    try:
        result = s3.test_connectivity_and_speed(config.access_key_id, config.secret_access_key, config.bucket_name, config.region)
    except Exception as exc:
        config.last_error = str(exc)[:1000]
        db.commit()
        return RedirectResponse(url=f"/settings?error={quote('Connectivity test failed: ' + str(exc)[:200])}#aws-provider", status_code=303)

    config.upload_mbps = result["upload_mbps"]
    config.download_mbps = result["download_mbps"]
    config.last_speed_test_at = datetime.now(timezone.utc)
    config.last_error = None
    db.commit()
    message = f"Connectivity OK - {result['upload_mbps']:.1f} Mbps up / {result['download_mbps']:.1f} Mbps down"
    return RedirectResponse(url=f"/settings?flash_status=ok&flash_message={quote(message)}#aws-provider", status_code=303)


@app.post("/settings/s3-storage/disconnect", dependencies=[Depends(require_admin)])
def disconnect_s3_storage(db: Session = Depends(get_db)):
    config = _get_s3_storage_config(db)
    if config:
        db.delete(config)
        db.commit()
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=AWS+storage+disconnected#aws-provider", status_code=303)


@app.get("/settings/s3-storage/browse", dependencies=[Depends(require_admin)])
def browse_s3_bucket(bucket: str, path: str = "", db: Session = Depends(get_db)):
    """Lists folders inside a bucket, for picking an archive's directory before
    the archive exists (mirrors browse_cloud_bucket for S3)."""
    config = _get_s3_storage_config(db)
    if not config or not config.access_key_id:
        raise HTTPException(status_code=400, detail="No AWS credentials are configured")
    try:
        bucket_name, _ = parse_s3_path(f"s3://{bucket.strip()}")
        rel = normalize_prefix(path)
        folders = s3.list_prefixes(config.access_key_id, config.secret_access_key, bucket_name, rel, config.region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not list bucket: {str(exc)[:160]}")
    return {"bucket": bucket_name, "path": rel.rstrip("/"), "folders": folders}


@app.post("/settings/s3-storage/archives", dependencies=[Depends(require_admin)])
def create_s3_archive(
    name: str = Form(...), path: str = Form(...), storage_class: str = Form(""), db: Session = Depends(get_db)
):
    """Adds an S3 archive: an s3://bucket/folder path libraries can back up
    to. All S3 archives share the one access key pair under Cloud Storage.
    Kept as a parallel route to create_cloud_archive rather than merged into
    it, so provider error messages stay independent (mirrors s3.py/gcs.py
    being kept as separate modules)."""
    name = name.strip()
    config = _get_s3_storage_config(db)
    if not config or not config.access_key_id:
        return RedirectResponse(url="/settings?error=Add+AWS+credentials+first#aws-provider", status_code=303)
    try:
        bucket, prefix = parse_s3_path(path)
    except ValueError as exc:
        return RedirectResponse(url=f"/settings?error={quote(str(exc))}#aws-provider", status_code=303)
    if not name:
        name = format_s3_path(bucket, prefix)
    storage_class = storage_class.strip().upper()
    if storage_class and storage_class not in s3.STORAGE_CLASSES:
        return RedirectResponse(url="/settings?error=Unknown+storage+class#aws-provider", status_code=303)
    try:
        s3.verify_bucket_access(config.access_key_id, config.secret_access_key, bucket, config.region)
    except Exception as exc:
        return RedirectResponse(url=f"/settings?error={quote('Cannot access bucket ' + bucket + ': ' + str(exc)[:160])}#aws-provider", status_code=303)

    db.add(
        StorageLocation(
            name=name,
            path=format_s3_path(bucket, prefix),
            location_type="s3",
            media_type="files",
            is_backup_target=True,
            storage_class=storage_class or None,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/settings?error=That+archive+path+already+exists#aws-provider", status_code=303)
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=S3+archive+added#archives", status_code=303)


@app.post("/settings/cloud-storage/set-backup", dependencies=[Depends(require_admin)])
def set_cloud_backup_target(db: Session = Depends(get_db)):
    config = _get_cloud_storage_config(db)
    if not config or not config.connected or not config.bucket_name:
        return RedirectResponse(url="/libraries?error=Connect+a+bucket+in+Settings+before+using+it+as+the+cloud+archive", status_code=303)

    # Independent of any local backup target - both can be active at once (see
    # set_backup_location above, which only clears other *local* targets).
    config.is_backup_target = True

    # The transfer pipeline (worker) still keys backup destinations off
    # StorageLocation - this shadow row (location_type="gcs") lets the existing
    # CopyJob/BackupArchive schema represent "the bucket" as a destination
    # without a parallel destination-type system running through every route.
    cloud_location = db.query(StorageLocation).filter_by(location_type="gcs").first()
    if not cloud_location:
        cloud_location = StorageLocation(location_type="gcs")
        db.add(cloud_location)
    cloud_location.name = f"Cloud: {config.bucket_name}"
    cloud_location.path = f"gcs://{config.bucket_name}"
    cloud_location.is_backup_target = True


    db.commit()
    return RedirectResponse(url="/libraries?flash_status=ok&flash_message=Bucket+set+as+cloud+archive", status_code=303)


@app.post("/settings/cloud-storage/unset-backup", dependencies=[Depends(require_admin)])
def unset_cloud_backup_target(db: Session = Depends(get_db)):
    config = _get_cloud_storage_config(db)
    if config:
        config.is_backup_target = False
    cloud_location = db.query(StorageLocation).filter_by(location_type="gcs").first()
    if cloud_location:
        cloud_location.is_backup_target = False
    db.commit()
    return RedirectResponse(url="/libraries", status_code=303)


@app.post("/settings/jellyfin", dependencies=[Depends(require_admin)])
def save_jellyfin_config(server_url: str = Form(...), api_key: str = Form(...), db: Session = Depends(get_db)):
    server_url = server_url.strip()
    api_key = api_key.strip()
    if not server_url or not api_key:
        return RedirectResponse(url="/integrations?error=Server+URL+and+API+key+are+required", status_code=303)

    config = db.query(JellyfinConfig).first()
    if config:
        config.server_url = server_url
        config.api_key = api_key
    else:
        db.add(JellyfinConfig(server_url=server_url, api_key=api_key))
    db.commit()
    return RedirectResponse(url="/integrations", status_code=303)


@app.post("/settings/jellyfin/delete", dependencies=[Depends(require_admin)])
def delete_jellyfin_config(db: Session = Depends(get_db)):
    config = db.query(JellyfinConfig).first()
    if config:
        db.delete(config)
        db.commit()
    return RedirectResponse(url="/integrations", status_code=303)


@app.post("/settings/jellyfin/test", dependencies=[Depends(require_admin)])
def test_jellyfin_connection(db: Session = Depends(get_db)):
    config = db.query(JellyfinConfig).first()
    if not config:
        return RedirectResponse(url="/integrations?flash_status=error&flash_message=No+Jellyfin+server+configured", status_code=303)

    result = jellyfin.test_connection(config.server_url, config.api_key)
    status = "ok" if result["ok"] else "error"
    return RedirectResponse(
        url=f"/integrations?flash_status={status}&flash_message={quote(result['message'])}", status_code=303
    )


@app.post("/settings/jellyfin/sync-user", dependencies=[Depends(require_admin)])
def save_jellyfin_sync_user(
    sync_user_id: str = Form(...),
    sync_user_name: str = Form(...),
    path_prefix_from: str = Form(...),
    path_prefix_to: str = Form(...),
    db: Session = Depends(get_db),
):
    config = db.query(JellyfinConfig).first()
    if not config:
        return RedirectResponse(url="/integrations?error=Save+a+Jellyfin+connection+first", status_code=303)

    config.sync_user_id = sync_user_id
    config.sync_user_name = sync_user_name
    config.path_prefix_from = path_prefix_from.strip() or "/media"
    config.path_prefix_to = path_prefix_to.strip() or "/mnt"
    db.commit()
    return RedirectResponse(url="/integrations", status_code=303)


@app.post("/settings/jellyfin/sync", dependencies=[Depends(require_admin)])
def run_jellyfin_sync(db: Session = Depends(get_db)):
    config = db.query(JellyfinConfig).first()
    if not config or not config.sync_user_id:
        return RedirectResponse(
            url="/integrations?flash_status=error&flash_message=Pick+a+Jellyfin+user+to+sync+first", status_code=303
        )

    try:
        result = sync_watch_data(db, config)
    except httpx.HTTPError as exc:
        return RedirectResponse(url=f"/integrations?flash_status=error&flash_message={quote(f'Sync failed: {exc}')}", status_code=303)

    message = f"Synced {result['matched']} of {result['items']} movies from Jellyfin ({result['unmatched']} unmatched)"
    return RedirectResponse(url=f"/integrations?flash_status=ok&flash_message={quote(message)}", status_code=303)


def _format_scan_message(result: dict) -> str:
    changed_files = result.get("changed_files", [])
    parts = [f"{result['found']} found", f"{result['added']} added"]
    if changed_files:
        parts.append(f"{len(changed_files)} changed")
    if result["missing"]:
        parts.append(f"{result['missing']} missing")
    if result.get("merged"):
        parts.append(f"{result['merged']} duplicate entries merged")
    message = "Scan complete: " + ", ".join(parts)
    if changed_files:
        names = [Path(p).name for p in changed_files[:5]]
        message += " - changed: " + ", ".join(names)
        if len(changed_files) > 5:
            message += f", and {len(changed_files) - 5} more"
    return message


@app.post("/scan-and-redirect", dependencies=[Depends(require_login)])
def scan_and_redirect(db: Session = Depends(get_db)):
    result = scan_library(db)
    return RedirectResponse(url=f"/catalog?message={quote(_format_scan_message(result))}", status_code=303)


@app.post("/settings/storage-locations/{location_id}/rescan", dependencies=[Depends(require_admin)])
def rescan_storage_location(location_id: int, db: Session = Depends(get_db)):
    """Scans just this one library (catalog.py:_scan_location), instead of
    every library like /scan-and-redirect - useful once there are enough
    libraries that a full rescan for a single change is wasteful."""
    location = db.get(StorageLocation, location_id)
    if not location:
        return RedirectResponse(url="/libraries?error=Library+not+found", status_code=303)
    if location.untracked:
        return RedirectResponse(
            url=f"/libraries?error={quote(location.name + ' is untracked - resume tracking before rescanning it.')}",
            status_code=303,
        )
    result = _scan_location(db, location)
    message = quote(f"{location.name}: " + _format_scan_message(result))
    return RedirectResponse(url=f"/libraries?flash_status=ok&flash_message={message}", status_code=303)


def _get_cloud_backup_location(db: Session) -> StorageLocation | None:
    return db.query(StorageLocation).filter_by(is_backup_target=True, location_type="gcs").first()


def _archive_for_location_id(db: Session, location_id: int | None) -> StorageLocation | None:
    """The archive (local or cloud) a library backs up to, or None if the
    library has no archive assigned."""
    library = db.get(StorageLocation, location_id) if location_id else None
    return db.get(StorageLocation, library.archive_location_id) if library and library.archive_location_id else None


def _archive_for_media_file(db: Session, media_file: MediaFile) -> StorageLocation | None:
    return _archive_for_location_id(db, media_file.storage_location_id)


def _v2_backup_record(db: Session, media_file_id: int, archive_id: int) -> BackupRecord | None:
    """The v2 (archive-based) backup record for a file at an archive, if any -
    v2 backups create BackupRecord rows but no CopyJob, so the legacy
    _latest_done_backup check alone can't see them."""
    return (
        db.query(BackupRecord)
        .filter_by(media_file_id=media_file_id, destination_storage_location_id=archive_id, status="done")
        .first()
    )


def _expand_folder_selection(db: Session, location: StorageLocation, folder_paths: list[str]) -> list[int]:
    """Resolves checked folder checkboxes from the Catalog's folder browser to
    every file nested under them, at any depth - checking a folder means its
    whole subtree, not just the files directly inside it."""
    root = Path(location.path)
    folder_parts_list = [tuple(p for p in fp.split("/") if p) for fp in folder_paths if fp]
    folder_parts_list = [parts for parts in folder_parts_list if parts]
    if not folder_parts_list:
        return []
    ids: list[int] = []
    for mf_id, path in db.query(MediaFile.id, MediaFile.path).filter_by(storage_location_id=location.id):
        try:
            rel_parts = Path(path).relative_to(root).parts
        except ValueError:
            continue
        for folder_parts in folder_parts_list:
            if len(rel_parts) > len(folder_parts) and rel_parts[: len(folder_parts)] == folder_parts:
                ids.append(mf_id)
                break
    return ids


def _resolve_bulk_selection(
    db: Session,
    media_file_ids: list[int],
    select_all_library: str | None,
    selected_folders: list[str] | None = None,
    folder_library: str | None = None,
) -> list[int]:
    """The Catalog's per-row checkboxes only ever cover what's actually
    rendered - a subset of the library once folder browsing or pagination is
    in play. "Select ALL in library" bypasses that and resolves here to every
    cataloged file under that StorageLocation (every folder, every page),
    overriding whatever checkboxes happened to be checked. Checked folder
    checkboxes (selected_folders, relative to folder_library's root) are
    expanded to their full subtree and added to the explicit file selection."""
    if select_all_library:
        location = db.query(StorageLocation).filter_by(name=select_all_library).one_or_none()
        if location:
            return [mf_id for (mf_id,) in db.query(MediaFile.id).filter_by(storage_location_id=location.id)]
    if selected_folders and folder_library:
        location = db.query(StorageLocation).filter_by(name=folder_library).one_or_none()
        if location:
            folder_ids = _expand_folder_selection(db, location, selected_folders)
            media_file_ids = list({*media_file_ids, *folder_ids})
    return media_file_ids


def _backup_files_to_archive(
    db: Session, media_file_ids: list[int], mode: str = "replace_older"
) -> tuple[int, int, str | None]:
    """Starts backups for files, each to *its own library's* archive: one
    tracked run per (library, archive). Returns (runs_started, files_skipped,
    first_error)."""
    files = db.query(MediaFile).filter(MediaFile.id.in_(media_file_ids)).all()
    groups: dict[tuple[int, int], list[int]] = {}
    skipped = 0
    error = None
    for mf in files:
        archive = _archive_for_media_file(db, mf)
        if archive is None:
            skipped += 1
            error = error or "some files are in libraries with no archive assigned"
            continue
        if archive.id == mf.storage_location_id:
            skipped += 1
            continue
        groups.setdefault((mf.storage_location_id, archive.id), []).append(mf.id)
    started = 0
    for (library_id, archive_id), ids in groups.items():
        runs = _start_batched_backup_runs(
            db, library_id, db.get(StorageLocation, archive_id), "selection", ids, mode=mode
        )
        started += len(runs)
    return started, skipped, error


@app.post("/movies/{media_file_id}/backup-cloud", dependencies=[Depends(require_login)])
def backup_media_file_to_cloud(media_file_id: int, mode: str = Form("replace_older"), db: Session = Depends(get_db)):
    """Backs a file up to its library's archive (cloud or local - the route name
    predates archives). Cloud archives get a tracked v2 run."""
    media_file = db.get(MediaFile, media_file_id)
    if not media_file:
        return RedirectResponse(url="/catalog?error=File+not+found", status_code=303)
    started, skipped, error = _backup_files_to_archive(db, [media_file_id], mode=_validate_run_mode(mode))
    if not started:
        reason = error or "nothing to back up"
        return RedirectResponse(url=f"/catalog?error={quote('Cannot back up ' + media_file.filename + ': ' + reason + '. Assign an archive to its library on the Libraries page.')}", status_code=303)
    return RedirectResponse(url=f"/copy-jobs?message={quote('Started backup of ' + media_file.filename)}", status_code=303)


def _start_restore_run(
    db: Session, archive: StorageLocation, file_ids: list[int], mode: str = "replace_older"
) -> RestoreRun:
    """Creates the tracking row (shows on Copy Jobs immediately, as "queued")
    and hands it to the worker's restore_run. mode is "replace_older" (don't
    clobber a local file newer than the backup, the default) or "replace_all"
    (overwrite unconditionally)."""
    run = RestoreRun(
        destination_storage_location_id=archive.id,
        scope="file" if len(file_ids) == 1 else "selection",
        file_ids=json.dumps(file_ids),
        mode=_validate_run_mode(mode),
        status="queued",
    )
    db.add(run)
    db.commit()
    try:
        run.celery_task_id = enqueue_restore_run(run.id)
        db.commit()
    except Exception as exc:  # broker down: don't leave a "queued" run that nothing will pick up
        run.status = "failed"
        run.error_message = f"could not queue the restore: {exc}"[:500]
        db.commit()
    return run


@app.post("/movies/{media_file_id}/restore-cloud", dependencies=[Depends(require_login)])
def restore_media_file_from_cloud(media_file_id: int, mode: str = Form("replace_older"), db: Session = Depends(get_db)):
    media_file = db.get(MediaFile, media_file_id)
    archive = _archive_for_media_file(db, media_file) if media_file else None
    if not media_file or not archive:
        return RedirectResponse(url="/catalog?error=No+archive+assigned+to+this+file%27s+library", status_code=303)
    if not _v2_backup_record(db, media_file_id, archive.id):
        return RedirectResponse(url="/catalog?error=No+completed+backup+to+restore+from", status_code=303)

    _start_restore_run(db, archive, [media_file_id], mode=_validate_run_mode(mode))
    return RedirectResponse(url="/copy-jobs", status_code=303)


@app.post("/movies/{media_file_id}/verify-cloud", dependencies=[Depends(require_login)])
def verify_media_file_cloud(media_file_id: int, deep: bool = False, db: Session = Depends(get_db)):
    media_file = db.get(MediaFile, media_file_id)
    archive = _archive_for_media_file(db, media_file) if media_file else None
    if not archive:
        return RedirectResponse(url="/catalog?error=No+archive+assigned+to+this+file%27s+library", status_code=303)

    if not _v2_backup_record(db, media_file_id, archive.id):
        return RedirectResponse(url="/catalog?error=No+completed+backup+to+verify", status_code=303)
    enqueue_verify_batches([media_file_id], archive.id, deep=deep)
    return RedirectResponse(url="/catalog", status_code=303)


@app.post("/movies/bulk-backup-cloud", dependencies=[Depends(require_login)])
def bulk_backup_media_files_to_cloud(
    media_file_ids: list[int] = Form(default=[]),
    select_all_library: str | None = Form(default=None),
    selected_folders: list[str] = Form(default=[]),
    folder_library: str | None = Form(default=None),
    mode: str = Form("replace_older"),
    db: Session = Depends(get_db),
):
    """Backs the selected files up, each to its own library's archive."""
    media_file_ids = _resolve_bulk_selection(db, media_file_ids, select_all_library, selected_folders, folder_library)
    if not media_file_ids:
        return RedirectResponse(url="/catalog?error=No+files+selected", status_code=303)
    started, skipped, error = _backup_files_to_archive(db, media_file_ids, mode=_validate_run_mode(mode))
    if not started:
        return RedirectResponse(
            url=f"/catalog?error={quote('Nothing started: ' + (error or 'no eligible files') + '. Assign an archive to each library on the Libraries page.')}",
            status_code=303,
        )
    message = f"Started {started} backup run(s)" + (f", skipped {skipped} file(s) (no archive assigned)" if skipped else "")
    return RedirectResponse(url=f"/copy-jobs?message={quote(message)}", status_code=303)


@app.post("/movies/bulk-restore-cloud", dependencies=[Depends(require_login)])
def bulk_restore_media_files_from_cloud(
    media_file_ids: list[int] = Form(default=[]),
    select_all_library: str | None = Form(default=None),
    selected_folders: list[str] = Form(default=[]),
    folder_library: str | None = Form(default=None),
    mode: str = Form("replace_older"),
    db: Session = Depends(get_db),
):
    media_file_ids = _resolve_bulk_selection(db, media_file_ids, select_all_library, selected_folders, folder_library)
    if not media_file_ids:
        return RedirectResponse(url="/catalog?error=No+files+selected", status_code=303)
    run_mode = _validate_run_mode(mode)
    by_archive: dict[int, tuple[StorageLocation, list[int]]] = {}
    skipped = 0
    for media_file_id in media_file_ids:
        media_file = db.get(MediaFile, media_file_id)
        archive = _archive_for_media_file(db, media_file) if media_file else None
        if not archive or not _v2_backup_record(db, media_file_id, archive.id):
            skipped += 1
            continue
        by_archive.setdefault(archive.id, (archive, []))[1].append(media_file_id)
    for archive, ids in by_archive.values():
        _start_restore_run(db, archive, ids, mode=run_mode)
    queued = sum(len(ids) for _, ids in by_archive.values())
    message = f"Started restore of {queued} file(s)" + (f", skipped {skipped} (no backup found)" if skipped else "") + ". Track it under Restore Runs."
    return RedirectResponse(url=f"/copy-jobs?message={quote(message)}", status_code=303)


@app.post("/movies/bulk-verify-cloud", dependencies=[Depends(require_login)])
def bulk_verify_media_files_cloud(
    media_file_ids: list[int] = Form(default=[]),
    select_all_library: str | None = Form(default=None),
    selected_folders: list[str] = Form(default=[]),
    folder_library: str | None = Form(default=None),
    deep: bool = False,
    db: Session = Depends(get_db),
):
    media_file_ids = _resolve_bulk_selection(db, media_file_ids, select_all_library, selected_folders, folder_library)
    if not media_file_ids:
        return RedirectResponse(url="/catalog?error=No+files+selected", status_code=303)
    queued = skipped = 0
    v2_by_archive: dict[int, list[int]] = {}
    for media_file_id in media_file_ids:
        media_file = db.get(MediaFile, media_file_id)
        archive = _archive_for_media_file(db, media_file) if media_file else None
        if not archive or not _v2_backup_record(db, media_file_id, archive.id):
            skipped += 1
            continue
        # Verified in batches per archive, so the cloud cost of a big
        # selection tracks archives, not files.
        v2_by_archive.setdefault(archive.id, []).append(media_file_id)
        queued += 1
    for archive_id, ids in v2_by_archive.items():
        enqueue_verify_batches(ids, archive_id, deep=deep)
    message = f"Queued verification of {queued} file(s)" + (f", skipped {skipped} (no backup found)" if skipped else "")
    return RedirectResponse(url=f"/catalog?message={quote(message)}", status_code=303)


# How long a "running" backup run may go without the worker writing progress
# before the UI calls it stuck. Generous: a single huge file is one uninterrupted
# hash/encrypt step with no callback in the middle.
RUN_STALL_SECONDS = 120
RUN_PHASE_LABELS = {
    "hashing": "Hashing files",
    "preparing": "Compressing / encrypting files",
    "packing": "Packing archives",
    "uploading": "Uploading",
    "restoring": "Restoring files",
    "syncing": "Syncing from bucket",
}


def _run_progress_view(run, now: datetime, kind: str = "backup") -> dict:
    """Everything the Backup Runs page needs to draw one run's live progress:
    the phase, a percentage where a total is known, a transfer rate and ETA
    (measured over the current phase), and how long since the worker last
    reported - so "slow" and "stuck" look different. Works for a BackupRun or
    a RestoreRun (`kind` namespaces the id). Pure: takes `now`."""
    active = run.status in ("queued", "running")
    phase = run.phase if run.status == "running" else None
    done, total = int(run.phase_done or 0), int(run.phase_total or 0)

    percent = min(100.0, done / total * 100) if phase and total > 0 else None
    rate = eta = None
    if phase and run.phase_started_at and done > 0:
        elapsed = (now - run.phase_started_at).total_seconds()
        if elapsed >= 3:
            rate = done / elapsed
            if total > done:
                eta = (total - done) / rate

    heartbeat_age = None
    if run.status == "running":
        beat = run.heartbeat_at or run.started_at
        if beat:
            heartbeat_age = max(0.0, (now - beat).total_seconds())

    return {
        "id": run.id,
        "key": f"{kind}-{run.id}",
        "status": run.status,
        "active": active,
        "phase": phase,
        "label": RUN_PHASE_LABELS.get(phase, phase) if phase else None,
        "unit": "bytes" if phase in ("uploading", "restoring", "syncing") else "files",
        "done": done if phase else None,
        "total": total if phase else None,
        "percent": percent,
        "rate": rate,
        "eta_seconds": eta,
        "heartbeat_age": heartbeat_age,
        "stalled": heartbeat_age is not None and heartbeat_age > RUN_STALL_SECONDS,
        "elapsed": (now - run.started_at).total_seconds() if run.status == "running" and run.started_at else None,
        "detail": run.detail,
        "error": run.error_message,
        "archives_done": getattr(run, "archives_done", 0) or 0,
        "archives_total": getattr(run, "archives_total", 0) or 0,
        "bytes_uploaded": int(getattr(run, "bytes_uploaded", 0) or 0),
    }


def _cancel_run(db: Session, run: BackupRun | RestoreRun | SyncRun) -> bool:
    """Revokes the Celery task backing a queued/running run and marks it
    failed. A queued run's task just never starts; a running one is sent
    SIGTERM (terminate=True) - the worker process dies, which also drops its
    dedicated advisory-lock connection (verified: pg_locks shows nothing held
    once the process is gone), so the next queued run picks up immediately.
    A hard-killed run's temp directory isn't guaranteed to be cleaned up
    (SIGTERM doesn't run Python's context-manager exit) - a known gap, not
    handled here."""
    if run.status not in ("queued", "running"):
        return False
    if run.celery_task_id:
        celery_client.control.revoke(run.celery_task_id, terminate=(run.status == "running"), signal="SIGTERM")
    run.status = "failed"
    run.error_message = "Cancelled by user"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    return True


@app.post("/copy-jobs/backup-runs/{run_id}/cancel", dependencies=[Depends(require_login)])
def cancel_backup_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(BackupRun, run_id)
    if not run or not _cancel_run(db, run):
        return RedirectResponse(url="/copy-jobs?error=Run+not+found+or+already+finished", status_code=303)
    return RedirectResponse(url="/copy-jobs?message=Backup+run+cancelled", status_code=303)


@app.post("/copy-jobs/restore-runs/{run_id}/cancel", dependencies=[Depends(require_login)])
def cancel_restore_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(RestoreRun, run_id)
    if not run or not _cancel_run(db, run):
        return RedirectResponse(url="/copy-jobs?error=Run+not+found+or+already+finished", status_code=303)
    return RedirectResponse(url="/copy-jobs?message=Restore+run+cancelled", status_code=303)


@app.post("/copy-jobs/sync-runs/{run_id}/cancel", dependencies=[Depends(require_login)])
def cancel_sync_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(SyncRun, run_id)
    if not run or not _cancel_run(db, run):
        return RedirectResponse(url="/copy-jobs?error=Run+not+found+or+already+finished", status_code=303)
    return RedirectResponse(url="/copy-jobs?message=Sync+run+cancelled", status_code=303)


def _clear_run(db: Session, run: BackupRun | RestoreRun | SyncRun) -> bool:
    """Deletes a finished-failed tracking row so it stops cluttering the Jobs
    page. Nothing else references these rows by foreign key (BackupRecord/
    BackupArchive are independent of which run wrote them), so this is just
    a row delete - it doesn't touch anything already backed up or restored."""
    if run.status != "failed":
        return False
    db.delete(run)
    db.commit()
    return True


@app.post("/copy-jobs/backup-runs/{run_id}/clear", dependencies=[Depends(require_login)])
def clear_backup_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(BackupRun, run_id)
    if not run or not _clear_run(db, run):
        return RedirectResponse(url="/copy-jobs?error=Run+not+found+or+not+failed", status_code=303)
    return RedirectResponse(url="/copy-jobs?message=Backup+run+cleared", status_code=303)


@app.post("/copy-jobs/restore-runs/{run_id}/clear", dependencies=[Depends(require_login)])
def clear_restore_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(RestoreRun, run_id)
    if not run or not _clear_run(db, run):
        return RedirectResponse(url="/copy-jobs?error=Run+not+found+or+not+failed", status_code=303)
    return RedirectResponse(url="/copy-jobs?message=Restore+run+cleared", status_code=303)


@app.post("/copy-jobs/sync-runs/{run_id}/clear", dependencies=[Depends(require_login)])
def clear_sync_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(SyncRun, run_id)
    if not run or not _clear_run(db, run):
        return RedirectResponse(url="/copy-jobs?error=Run+not+found+or+not+failed", status_code=303)
    return RedirectResponse(url="/copy-jobs?message=Sync+run+cleared", status_code=303)


@app.post("/copy-jobs/clear-all-failed", dependencies=[Depends(require_login)])
def clear_all_failed_runs(db: Session = Depends(get_db)):
    n_backup = db.query(BackupRun).filter_by(status="failed").delete(synchronize_session=False)
    n_restore = db.query(RestoreRun).filter_by(status="failed").delete(synchronize_session=False)
    n_sync = db.query(SyncRun).filter_by(status="failed").delete(synchronize_session=False)
    db.commit()
    total = n_backup + n_restore + n_sync
    return RedirectResponse(url=f"/copy-jobs?message=Cleared+{total}+failed+run(s)", status_code=303)


@app.post("/copy-jobs/backup-runs/clear-failed", dependencies=[Depends(require_login)])
def clear_failed_backup_runs(db: Session = Depends(get_db)):
    n = db.query(BackupRun).filter_by(status="failed").delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(url=f"/copy-jobs?message=Cleared+{n}+failed+backup+run(s)", status_code=303)


@app.post("/copy-jobs/restore-runs/clear-failed", dependencies=[Depends(require_login)])
def clear_failed_restore_runs(db: Session = Depends(get_db)):
    n = db.query(RestoreRun).filter_by(status="failed").delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(url=f"/copy-jobs?message=Cleared+{n}+failed+restore+run(s)", status_code=303)


@app.post("/copy-jobs/sync-runs/clear-failed", dependencies=[Depends(require_login)])
def clear_failed_sync_runs(db: Session = Depends(get_db)):
    n = db.query(SyncRun).filter_by(status="failed").delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(url=f"/copy-jobs?message=Cleared+{n}+failed+sync+run(s)", status_code=303)


@app.get("/copy-jobs/runs/status", dependencies=[Depends(require_login)])
def backup_runs_status(db: Session = Depends(get_db)) -> dict:
    """Live progress for the Backup Runs table; polled by copy_jobs.html."""
    now = datetime.now(timezone.utc)
    runs = db.query(BackupRun).order_by(BackupRun.created_at.desc()).limit(25).all()
    restores = db.query(RestoreRun).order_by(RestoreRun.created_at.desc()).limit(25).all()
    syncs = db.query(SyncRun).order_by(SyncRun.created_at.desc()).limit(25).all()
    return {
        "runs": [_run_progress_view(run, now) for run in runs]
        + [_run_progress_view(run, now, kind="restore") for run in restores]
        + [_run_progress_view(run, now, kind="sync") for run in syncs]
    }


@app.get("/copy-jobs", response_class=HTMLResponse)
def copy_jobs_page(
    request: Request, message: str | None = None, db: Session = Depends(get_db), username: str = Depends(require_login)
):
    run_rows = (
        db.query(BackupRun, StorageLocation)
        .join(StorageLocation, BackupRun.source_storage_location_id == StorageLocation.id)
        .order_by(BackupRun.created_at.desc())
        .limit(25)
        .all()
    )
    runs = []
    for run, source in run_rows:
        skipped = json.loads(run.skipped_json) if run.skipped_json else []
        backed_up = json.loads(run.backed_up_json) if run.backed_up_json else []
        counts: dict[str, int] = {}
        for entry in skipped:
            counts[entry["reason"]] = counts.get(entry["reason"], 0) + 1
        runs.append({"run": run, "source": source, "skip_counts": counts, "skipped": skipped, "backed_up": backed_up})
    restore_rows = (
        db.query(RestoreRun, StorageLocation)
        .join(StorageLocation, RestoreRun.destination_storage_location_id == StorageLocation.id)
        .order_by(RestoreRun.created_at.desc())
        .limit(25)
        .all()
    )
    restores = [
        {"run": run, "archive": archive, "results": json.loads(run.results_json) if run.results_json else []}
        for run, archive in restore_rows
    ]
    sync_rows = (
        db.query(SyncRun, StorageLocation)
        .join(StorageLocation, SyncRun.library_storage_location_id == StorageLocation.id)
        .order_by(SyncRun.created_at.desc())
        .limit(25)
        .all()
    )
    syncs = [
        {"run": run, "library": library, "results": json.loads(run.results_json) if run.results_json else []}
        for run, library in sync_rows
    ]
    runs_active = any(r["run"].status in ("queued", "running") for r in runs + restores + syncs)
    failed_backup_count = db.query(BackupRun).filter_by(status="failed").count()
    failed_restore_count = db.query(RestoreRun).filter_by(status="failed").count()
    failed_sync_count = db.query(SyncRun).filter_by(status="failed").count()

    return templates.TemplateResponse(
        request,
        "copy_jobs.html",
        {
            "active": "copy_jobs",
            "username": username,
            "message": message,
            "runs": runs,
            "restores": restores,
            "syncs": syncs,
            "runs_active": runs_active,
            "failed_backup_count": failed_backup_count,
            "failed_restore_count": failed_restore_count,
            "failed_sync_count": failed_sync_count,
        },
    )


@app.get("/scheduled-backups", response_class=HTMLResponse)
def scheduled_backups_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    username: str = Depends(require_login),
):
    libraries = (
        db.query(StorageLocation)
        .filter(StorageLocation.location_type == "local", StorageLocation.is_backup_target.is_(False))
        .order_by(StorageLocation.name)
        .all()
    )
    schedules = {s.source_storage_location_id: s for s in db.query(BackupSchedule).all()}
    archive_by_id = {
        loc.id: loc for loc in db.query(StorageLocation).filter(StorageLocation.is_backup_target.is_(True)).all()
    }
    rows = [
        {
            "library": lib,
            "archive": archive_by_id.get(lib.archive_location_id),
            "schedule": schedules.get(lib.id),
        }
        for lib in libraries
    ]
    return templates.TemplateResponse(
        request,
        "scheduled_backups.html",
        {
            "active": "scheduled_backups",
            "username": username,
            "message": message,
            "error": error,
            "rows": rows,
        },
    )


@app.post("/scheduled-backups/{location_id}", dependencies=[Depends(require_login)])
def save_backup_schedule(
    location_id: int,
    enabled: bool = Form(False),
    frequency: str = Form("daily"),
    time_of_day: str = Form(...),
    days_of_week: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    library = db.get(StorageLocation, location_id)
    if not library:
        return RedirectResponse(url="/scheduled-backups?error=Library+not+found", status_code=303)
    if not library.archive_location_id:
        return RedirectResponse(
            url=f"/scheduled-backups?error={quote(library.name + ' has no archive assigned - set one on Libraries first')}",
            status_code=303,
        )
    if frequency not in VALID_FREQUENCIES:
        return RedirectResponse(url="/scheduled-backups?error=Invalid+frequency", status_code=303)

    try:
        hour, minute = (int(part) for part in time_of_day.split(":"))
        parsed_time = time_cls(hour=hour, minute=minute)
    except ValueError:
        return RedirectResponse(url="/scheduled-backups?error=Invalid+time", status_code=303)

    days_value = ",".join(sorted({str(d) for d in days_of_week})) if frequency == "weekly" else None
    if frequency == "weekly" and not parse_days_of_week(days_value):
        return RedirectResponse(url="/scheduled-backups?error=Pick+at+least+one+day+for+a+weekly+schedule", status_code=303)

    schedule = db.query(BackupSchedule).filter_by(source_storage_location_id=location_id).first()
    if not schedule:
        schedule = BackupSchedule(source_storage_location_id=location_id)
        db.add(schedule)

    schedule.enabled = enabled
    schedule.frequency = frequency
    schedule.time_of_day = parsed_time
    schedule.days_of_week = days_value
    now = datetime.now(timezone.utc)
    schedule.next_run_at = compute_next_run(frequency, parsed_time, days_value, now) if enabled else None
    db.commit()

    message = f"Saved schedule for {library.name}."
    return RedirectResponse(url=f"/scheduled-backups?message={quote(message)}", status_code=303)


@app.post("/scheduled-backups/{location_id}/delete", dependencies=[Depends(require_login)])
def delete_backup_schedule(location_id: int, db: Session = Depends(get_db)):
    db.query(BackupSchedule).filter_by(source_storage_location_id=location_id).delete()
    db.commit()
    return RedirectResponse(url="/scheduled-backups?message=Schedule+removed", status_code=303)


def _restart_service_via_terminator(service: str) -> str | None:
    """Calls terminator to restart a compose service. Returns None on
    success, or a user-facing error message on failure."""
    try:
        response = httpx.post(
            f"{TERMINATOR_URL}/services/{service}/restart",
            headers={"X-Terminator-Key": app_settings.terminator_api_key},
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        return f"Could not reach terminator: {exc}"

    if response.status_code != 200:
        return f"Restart failed: {response.text}"
    return None


@app.post("/settings/services/{service}/restart", dependencies=[Depends(require_admin)])
def restart_service(service: str):
    error = _restart_service_via_terminator(service)
    if error:
        return RedirectResponse(url=f"/settings?flash_status=error&flash_message={quote(error)}", status_code=303)
    return RedirectResponse(url=f"/settings?flash_status=ok&flash_message={quote(f'Restarted {service}')}", status_code=303)


def _get_tls_config(db: Session) -> TlsConfig:
    config = db.query(TlsConfig).first()
    if not config:
        config = TlsConfig()
        db.add(config)
        db.commit()
    return config


@app.post("/settings/tls/domains", dependencies=[Depends(require_admin)])
def set_tls_domains(domains: str = Form(""), external_url: str = Form(""), db: Session = Depends(get_db)):
    config = _get_tls_config(db)
    config.domains = domains.strip() or None
    config.external_url = external_url.strip() or None
    db.commit()

    tls.write_nginx_config(config.domains or "", config.redirect_http)
    error = _restart_service_via_terminator("nginx")
    if error:
        return RedirectResponse(
            url=f"/settings?flash_status=error&flash_message={quote(f'Domains saved but nginx restart failed: {error}')}",
            status_code=303,
        )
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Domains+saved", status_code=303)


@app.post("/settings/tls/upload", dependencies=[Depends(require_admin)])
async def upload_tls_cert(
    cert_pem: str = Form(...),
    key_pem: str = Form(...),
    redirect_http: bool = Form(False),
    db: Session = Depends(get_db),
):
    cert_pem = cert_pem.strip().encode()
    key_pem = key_pem.strip().encode()
    try:
        tls.validate_cert_key_pair(cert_pem, key_pem)
    except ValueError as exc:
        return RedirectResponse(url=f"/settings?error={quote(str(exc))}", status_code=303)

    tls.write_active_cert(cert_pem, key_pem)

    config = _get_tls_config(db)
    config.is_custom = True
    config.redirect_http = redirect_http
    config.cert_uploaded_at = datetime.now(timezone.utc)
    db.commit()

    tls.write_nginx_config(config.domains or "", config.redirect_http)
    error = _restart_service_via_terminator("nginx")
    if error:
        return RedirectResponse(
            url=f"/settings?flash_status=error&flash_message={quote(f'Cert saved but nginx restart failed: {error}')}",
            status_code=303,
        )
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Certificate+installed", status_code=303)


@app.post("/settings/tls/revert", dependencies=[Depends(require_admin)])
def revert_tls_cert(db: Session = Depends(get_db)):
    try:
        tls.revert_to_self_signed()
    except ValueError as exc:
        return RedirectResponse(url=f"/settings?error={quote(str(exc))}", status_code=303)

    config = _get_tls_config(db)
    config.is_custom = False
    config.cert_uploaded_at = None
    db.commit()

    error = _restart_service_via_terminator("nginx")
    if error:
        return RedirectResponse(
            url=f"/settings?flash_status=error&flash_message={quote(f'Reverted but nginx restart failed: {error}')}",
            status_code=303,
        )
    return RedirectResponse(url="/settings?flash_status=ok&flash_message=Reverted+to+self-signed+certificate", status_code=303)
