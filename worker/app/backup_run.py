"""The v2 backup run orchestrator (docs/cfa-spec.md section 6).

One Celery task performs a complete run: refuse if unusable, take the
advisory lock, build the file list from the catalog, hash, pack, upload,
verify, index, record, clean up - in exactly section 6's order. This is the
first step in the v2 rework that changes runtime behaviour; everything it
calls (fingerprint.hash_file, packer.pack, backup_index.build_index,
storage.upload_and_confirm) was built and tested in steps 2-5 without being
wired up anywhere.

`worker/app/tasks.py` owns the legacy pipeline (_record_backup, the encrypted
GCS path, the legacy restore functions) and is untouched by this module - see
"Do Not Touch" in docs/backup-plan/steps/06-backup-run.md.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.archive_paths import is_gcs_path, is_s3_path, normalize_prefix, parse_gcs_path, parse_s3_path
from app.backup_index import archive_object_key, build_index, index_object_key
from app.celery_app import app
from app.db import SessionLocal, engine
from app import gcs
from app.compression import COMPRESSION_GZIP, DEFAULT_LEVEL, compress_if_worthwhile
from app.encryption import (
    BLOB_CHUNK_SIZE,
    BLOB_HEADER_SIZE,
    derive_content_id,
    derive_file_key,
    derive_key_check,
    derive_master_key,
    derive_sha256_seal_key,
    encrypt_blob,
    encrypt_paths,
    encrypt_sha256,
    encrypted_chunk_size,
)
from app.fingerprint import FileChangedDuringRead, hash_file
from app.notify import notify
from app.models import (
    BackupArchive,
    BackupEncryptionConfig,
    BackupKeyVersion,
    BackupRecord,
    BackupRecordArchive,
    BackupRecordContentId,
    BackupRun,
    CloudStorageConfig,
    MediaFile,
    StorageLocation,
    TransferConfig,
)
from app.packer import FileToPack, PackedArchive, PackedMember, pack, split_part_sizes
from app.storage import StorageBackend, StorageConfigError, backend_for_destination, upload_and_confirm

logger = logging.getLogger(__name__)

# Arbitrary but stable - "MBRN" packed into an int32. Any stable int works;
# this one is just easy to recognize in pg_locks.
BACKUP_RUN_LOCK_KEY = 0x4D42524E


class BackupRunRefused(Exception):
    """Raised by the section 6.1-equivalent refusal checks, before any file
    is touched or the advisory lock is taken."""


def _parse_exclude_globs(raw: str | None) -> list[str]:
    """Mirrors web/app/backup_settings.py:parse_exclude_globs. Duplicated
    rather than imported because web/ and worker/ are not a shared package
    (see CLAUDE.md) - keep this identical to the web copy if either changes."""
    if not raw:
        return []
    globs: list[str] = []
    for line in raw.splitlines():
        pattern = line.strip()
        if pattern and pattern not in globs:
            globs.append(pattern)
    return globs


def _relative_path(source_location: StorageLocation, media_file: MediaFile) -> str:
    """Mirrors app.tasks._relative_source_path's fallback behaviour, but
    returns a str with '/' separators (the tar member name and the
    fnmatch subject), not a Path."""
    try:
        return Path(media_file.path).relative_to(Path(source_location.path)).as_posix()
    except ValueError:
        return media_file.filename


def _is_excluded(rel_path: str, patterns: list[str]) -> bool:
    # fnmatch's '*' crosses '/' (unlike pathlib's match or glob.glob), so a
    # pattern like "**/.cache/**" behaves as intended against a multi-segment
    # relative path even though fnmatch has no special meaning for '**'.
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


def _refuse_if_unusable(db, destination: StorageLocation | None) -> None:
    """Section 6's pre-flight refusals, in spec order, that don't depend on
    which cloud provider the archive uses - provider-specific config
    validation happens in _backend_for_destination, called right after."""
    if destination is None:
        raise BackupRunRefused("destination storage location does not exist")

    encryption_config = db.query(BackupEncryptionConfig).first()
    if encryption_config and encryption_config.enabled:
        if not encryption_config.password or not encryption_config.kdf_salt:
            raise BackupRunRefused("backup encryption is enabled but no backup password is configured")

    if not destination.is_backup_target:
        raise BackupRunRefused(f"storage location {destination.id} is not a backup target")


def _backend_for_destination(db, destination: StorageLocation, storage_class: str | None = None) -> StorageBackend:
    """Thin wrapper over storage.backend_for_destination (the single dispatch
    point for s3:// vs gs:///gcs://, shared with restore_run.py, tasks.py's
    verify, and sync_run.py), translating its StorageConfigError into this
    module's BackupRunRefused."""
    try:
        return backend_for_destination(db, destination, storage_class=storage_class)
    except StorageConfigError as exc:
        raise BackupRunRefused(str(exc)) from exc


@dataclass
class _Candidate:
    media_file: MediaFile
    file_to_pack: FileToPack


def _record_archives_present(
    record: BackupRecord,
    archive_paths_by_record_id: dict[int, list[str]] | None,
    known_object_keys: set[str] | None,
) -> bool:
    """True unless known_object_keys is available and at least one archive
    object linked to this record is confirmed missing from the bucket
    listing - guards against out-of-band deletion (an archive object removed
    directly from the bucket) making a file look unchanged/adoptable forever.
    known_object_keys=None means "no listing was taken" (e.g. tests that
    don't pass one) - treated as always present, matching pre-fix behaviour."""
    if known_object_keys is None or archive_paths_by_record_id is None:
        return True
    paths = archive_paths_by_record_id.get(record.id)
    if not paths:
        return True
    return all(path in known_object_keys for path in paths)


def _is_unchanged(
    media_file: MediaFile,
    current_stat,
    record: BackupRecord | None,
    archive_paths_by_record_id: dict[int, list[str]] | None = None,
    known_object_keys: set[str] | None = None,
    force_all: bool = False,
) -> bool:
    """Section 6.1's quick check: skip a file whose size and mtime both still
    match its last successful v2 backup at this destination. Both fields
    must match - either one differing (a real edit that happens to preserve
    mtime, or a touch with no content change) forces a re-backup. A record
    with mtime_ns still NULL predates step 06 (a legacy-format row reusing
    the same (media_file_id, destination) key) and is never trusted here.

    known_object_keys, when given (together with archive_paths_by_record_id),
    is a set of every object key confirmed present under this destination's
    prefix (one batched listing per run - see _run_locked); a record whose
    linked archive object isn't in it means the archive was deleted
    out-of-band (e.g. directly from the bucket), so it is never treated as
    unchanged even if local mtime/size still match.

    force_all is BackupRun.mode == "replace_all": when set, every file is
    treated as changed regardless of record/mtime/size, bypassing this
    shortcut entirely (docs/CHANGES.md 260927). Content-based dedup
    (_adopt_existing_copies) still runs afterwards - force_all only defeats
    the cheap bookkeeping-based skip, not the cloud-cost dedup."""
    if force_all:
        return False
    if record is None or record.mtime_ns is None:
        return False
    if not (current_stat.st_mtime_ns == record.mtime_ns and current_stat.st_size == media_file.size_bytes):
        return False
    return _record_archives_present(record, archive_paths_by_record_id, known_object_keys)


def _upsert_content_id(db, record: BackupRecord, key_version_id: int, content_id: str) -> None:
    """One row per (record, key-version): accumulates across rotations
    (docs/backup-plan/DEVIATIONS.md) rather than overwriting, so dedup keeps
    recognizing this content under a key version it was previously computed
    for even after the key has since rotated again."""
    row = (
        db.query(BackupRecordContentId)
        .filter_by(backup_record_id=record.id, key_version_id=key_version_id)
        .one_or_none()
    )
    if row is None:
        db.add(BackupRecordContentId(backup_record_id=record.id, key_version_id=key_version_id, content_id=content_id))
    else:
        row.content_id = content_id


def _update_run(db, run_id: int | None, **fields) -> None:
    """Best-effort progress write to the tracking row. Never raises: a run's
    real work must not fail because its status row couldn't be updated (or
    doesn't exist - runs started from tests/CLI have no run_id)."""
    if run_id is None:
        return
    try:
        run = db.get(BackupRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        db.commit()
    except Exception:  # pragma: no cover - logged, never fatal
        logger.exception("could not update backup run %s", run_id)
        db.rollback()


class _Progress:
    """Live progress for one run, written to its BackupRun row so the UI can
    show a bar, a rate, an ETA and a heartbeat. Writes are throttled (the
    upload callback fires per 8 MiB chunk, hashing per file); a phase change
    or flush() always writes. Best-effort like _update_run: it never raises.
    With no run_id (tests/CLI) it does nothing."""

    def __init__(self, db, run_id: int | None, *, min_interval: float = 1.0, clock=time.monotonic, update=None):
        self._db = db
        self._run_id = run_id
        # How rows are written; BackupRun's by default, restore runs pass their own.
        # Looked up at call time (not bound here) so tests can patch _update_run.
        self._update = update
        self._min_interval = min_interval
        self._clock = clock
        self._done = 0
        self._last_write = float("-inf")

    def _write(self, db, run_id, **fields) -> None:
        (self._update or _update_run)(db, run_id, **fields)

    def phase(self, name: str, total: int = 0) -> None:
        now = datetime.now(timezone.utc)
        self._done = 0
        self._last_write = self._clock()
        self._write(
            self._db,
            self._run_id,
            phase=name,
            phase_done=0,
            phase_total=total,
            phase_started_at=now,
            heartbeat_at=now,
        )

    def advance(self, done: int) -> None:
        self._done = done
        if self._clock() - self._last_write >= self._min_interval:
            self.flush()

    def flush(self) -> None:
        self._last_write = self._clock()
        self._write(self._db, self._run_id, phase_done=self._done, heartbeat_at=datetime.now(timezone.utc))

    def finish(self) -> None:
        self._write(self._db, self._run_id, phase=None, phase_done=0, phase_total=0, heartbeat_at=datetime.now(timezone.utc))


def _build_file_list(
    db,
    source_location: StorageLocation,
    destination_storage_location_id: int,
    file_ids: list[int] | None = None,
    progress: _Progress | None = None,
    known_object_keys: set[str] | None = None,
    force_all: bool = False,
) -> tuple[list[_Candidate], list[dict]]:
    """Section 6 steps 1-2: read the catalog (no filesystem walk), skip
    excluded and unchanged files (section 6.1), hash the rest. Returns the
    packable candidates plus a list of skip records for the summary.

    known_object_keys (see _run_locked) is one batched bucket listing for
    this run, used to make sure an "unchanged" file's archive object still
    actually exists before trusting the mtime/size shortcut.

    force_all is BackupRun.mode == "replace_all" - see _is_unchanged."""
    patterns = _parse_exclude_globs(source_location.exclude_globs)
    query = db.query(MediaFile).filter_by(storage_location_id=source_location.id)
    if file_ids is not None:
        # An explicit selection (single file / checked files): only those, and
        # only if they belong to this source location.
        query = query.filter(MediaFile.id.in_(file_ids))
    media_files = query.order_by(MediaFile.path).all()
    # One query for every relevant BackupRecord, not one per file - a real
    # library can have thousands of rows and this runs on every backup.
    records_by_media_file_id = {
        r.media_file_id: r
        for r in db.query(BackupRecord)
        .filter_by(destination_storage_location_id=destination_storage_location_id)
        .all()
    }
    archive_paths_by_record_id: dict[int, list[str]] = {}
    if known_object_keys is not None and records_by_media_file_id:
        record_ids = [r.id for r in records_by_media_file_id.values()]
        for i in range(0, len(record_ids), 500):
            for link, archive in (
                db.query(BackupRecordArchive, BackupArchive)
                .join(BackupArchive, BackupArchive.id == BackupRecordArchive.backup_archive_id)
                .filter(BackupRecordArchive.backup_record_id.in_(record_ids[i : i + 500]))
            ):
                archive_paths_by_record_id.setdefault(link.backup_record_id, []).append(archive.path)

    candidates: list[_Candidate] = []
    skipped: list[dict] = []

    if progress:
        progress.phase("hashing", len(media_files))
    for index, media_file in enumerate(media_files):
        if progress:
            progress.advance(index)
        rel_path = _relative_path(source_location, media_file)

        if _is_excluded(rel_path, patterns):
            skipped.append({"path": media_file.path, "reason": "excluded"})
            continue

        if media_file.is_missing:
            # Already known gone as of the last scan (catalog.py's
            # is_missing flag) - not a new problem discovered by this run,
            # so it's exempt from the partial-run check below, unlike a
            # "missing" file this run finds unexpectedly (stat() failing
            # further down). Otherwise a library with any stale/deleted-
            # locally entries would never show "done" again.
            skipped.append({"path": media_file.path, "reason": "already_missing"})
            continue

        source_path = Path(media_file.path)
        try:
            current_stat = source_path.stat()
        except (FileNotFoundError, NotADirectoryError):
            logger.warning("backup run: skipping missing file %s", media_file.path)
            skipped.append({"path": media_file.path, "reason": "missing"})
            continue

        record = records_by_media_file_id.get(media_file.id)
        if _is_unchanged(
            media_file,
            current_stat,
            record,
            archive_paths_by_record_id=archive_paths_by_record_id if record else None,
            known_object_keys=known_object_keys,
            force_all=force_all,
        ):
            skipped.append({"path": media_file.path, "reason": "unchanged"})
            continue

        try:
            hashed = hash_file(source_path)
        except FileChangedDuringRead as exc:
            logger.warning("backup run: skipping changed file: %s", exc)
            skipped.append({"path": media_file.path, "reason": "changed_during_read"})
            continue

        candidates.append(
            _Candidate(
                media_file=media_file,
                file_to_pack=FileToPack(
                    source_path=source_path,
                    rel_path=rel_path,
                    sha256=hashed.sha256,
                    size_bytes=hashed.size_bytes,
                    mtime_ns=hashed.mtime_ns,
                ),
            )
        )

    if progress:
        progress.advance(len(media_files))
        progress.flush()
    return candidates, skipped


def _get_master_key(db) -> bytes | None:
    """The global key, or None when backup encryption is off. Mirrors
    tasks._get_master_key; this module doesn't import tasks.py."""
    config = db.query(BackupEncryptionConfig).first()
    if not config or not config.enabled or not config.password or not config.kdf_salt:
        return None
    return derive_master_key(config.password, bytes.fromhex(config.kdf_salt))


def _resolve_target(
    destination: StorageLocation, source: StorageLocation, cloud_config: CloudStorageConfig | None
) -> tuple[str, str]:
    """(bucket, key prefix) a run writes under: the archive's gs://bucket/folder
    or s3://bucket/folder plus - when this library is assigned to that
    archive - the library's own subfolder. A gs:// archive predating gs://
    paths falls back to the cloud config's single legacy bucket/prefix
    instead; an s3:// archive always carries its own bucket (S3 support was
    added after gs:// paths, so there's no equivalent legacy fallback for it)."""
    if is_s3_path(destination.path):
        try:
            bucket, base = parse_s3_path(destination.path)
            sub = source.archive_subpath if source.archive_location_id == destination.id else ""
            return bucket, normalize_prefix(base, sub)
        except ValueError as exc:
            raise BackupRunRefused(f"archive {destination.name!r}: {exc}") from exc
    if not is_gcs_path(destination.path):
        if cloud_config is None:
            raise BackupRunRefused("no Google Cloud service account is configured")
        return cloud_config.bucket_name, normalize_prefix(cloud_config.prefix)
    try:
        bucket, base = parse_gcs_path(destination.path)
        sub = source.archive_subpath if source.archive_location_id == destination.id else ""
        return bucket, normalize_prefix(base, sub)
    except ValueError as exc:
        raise BackupRunRefused(f"archive {destination.name!r}: {exc}") from exc


def _current_key_version_id(db) -> int | None:
    """Id of the BackupKeyVersion row for the current passphrase (the web app
    keeps these in step with BackupEncryptionConfig - see web/app/key_versions.py),
    creating it if an install predates key versions."""
    config = db.query(BackupEncryptionConfig).first()
    if not config or not config.password or not config.kdf_salt:
        return None
    row = (
        db.query(BackupKeyVersion)
        .filter_by(password=config.password, kdf_salt=config.kdf_salt, retired_at=None)
        .order_by(BackupKeyVersion.id.desc())
        .first()
    )
    if row is None:
        row = BackupKeyVersion(
            password=config.password, kdf_salt=config.kdf_salt, created_at=config.updated_at or datetime.now(timezone.utc)
        )
        db.add(row)
        db.commit()
    return row.id


def _blob_chunk_size(max_size_bytes: int) -> int:
    """Encryption chunk size for a run: BLOB_CHUNK_SIZE, shrunk (in whole MiB)
    so at least ~4 chunks fit under the payload ceiling. Without this, a small
    max_size (the UI allows down to 64 MiB) would be smaller than one 64 MiB
    chunk and boundary-aligned splitting would be impossible. The chunk size
    is recorded in each blob's header, so restore never needs to know it."""
    ceiling = max_size_bytes - 1024 * 1024
    quarter_mib = (ceiling // 4) // (1024 * 1024) * (1024 * 1024)
    return min(BLOB_CHUNK_SIZE, max(1024 * 1024, quarter_mib))


def _prepare_candidates(
    candidates: list[_Candidate],
    work_dir: Path,
    *,
    master_key: bytes | None,
    chunk_size: int,
    compression_level: int | None,
    content_id_key: bytes | None = None,
    progress: _Progress | None = None,
) -> list[FileToPack]:
    """Turns each unique-hash file into the bytes that will actually be packed,
    once per hash: optionally gzipped (compression_level not None; skipped for
    files that don't shrink enough - see compression.py), then optionally
    encrypted into work_dir/<sha256>.file (key: derived from the global key +
    the file's hash - the *original* file's hash, so compression never touches
    the key). Returns the FileToPack list the packer should see: source and
    size are the prepared bytes', rel_path stays the real path so the index
    can report (or, when encrypted, seal) it, and `compression` records what
    was done.

    Tar member names: encrypted -> <content_id>.file (a keyed HMAC, never the
    real sha256 - see encryption.derive_content_id and
    docs/backup-plan/DEVIATIONS.md); plain + gzipped -> <rel_path>.gz;
    otherwise the real relative path. content_id_key is separate from
    master_key (which here means "encrypt this run's blobs") because it's
    only set when this run has a real key_version_id to tag the id with -
    see _run_locked's call site."""
    work_dir.mkdir(parents=True, exist_ok=True)
    prepared: dict[str, tuple[Path, int, str | None]] = {}
    packable: list[FileToPack] = []
    if progress:
        progress.phase("preparing", len(candidates))
    for index, c in enumerate(candidates):
        if progress:
            progress.advance(index)
        f = c.file_to_pack
        content_id = derive_content_id(content_id_key, f.sha256) if content_id_key is not None else None
        if f.sha256 not in prepared:
            source, size, compression = f.source_path, f.size_bytes, None
            gz_path = None
            if compression_level is not None:
                candidate_gz = work_dir / f"{f.sha256}.gz"
                gz_size = compress_if_worthwhile(f.source_path, candidate_gz, f.size_bytes, compression_level)
                if gz_size is not None:
                    source, size, compression, gz_path = candidate_gz, gz_size, COMPRESSION_GZIP, candidate_gz
            if master_key is not None:
                blob = work_dir / f"{f.sha256}.file"
                size = encrypt_blob(source, blob, derive_file_key(master_key, f.sha256), chunk_size=chunk_size)
                source = blob
                if gz_path is not None:
                    gz_path.unlink(missing_ok=True)  # the blob supersedes it; keep peak disk down
            prepared[f.sha256] = (source, size, compression)
        source, size, compression = prepared[f.sha256]
        if master_key is not None:
            # Falls back to sha256 only if content_id_key wasn't set for an
            # encrypted run (shouldn't happen via _run_locked - it always
            # passes content_id_key alongside an encrypting master_key), so
            # a tar member is never left unnamed.
            member_stem = f"{content_id or f.sha256}.file"
        elif compression is not None:
            member_stem = f"{f.rel_path}.gz"
        else:
            member_stem = None
        packable.append(
            FileToPack(
                source_path=source,
                rel_path=f.rel_path,
                sha256=f.sha256,
                size_bytes=size,
                mtime_ns=f.mtime_ns,
                member_stem=member_stem,
                compression=compression,
                content_id=content_id,
            )
        )
    if progress:
        progress.advance(len(candidates))
        progress.flush()
    return packable


def _split_args(encrypted: bool, max_size_bytes: int) -> dict:
    """Extra split_part_sizes/pack kwargs: encrypted blobs are cut on chunk
    boundaries (see encryption.py), plain files use the balanced split."""
    if not encrypted:
        return {}
    return {
        "align_unit": encrypted_chunk_size(_blob_chunk_size(max_size_bytes)),
        "align_head": BLOB_HEADER_SIZE,
    }


def _archive_length_for_member(member: PackedMember, *, max_size_bytes: int, encrypted: bool = False) -> int:
    """PackedMember.size_bytes is the whole file's size even on a part member
    (section 5 keys entries by the whole file's hash), so it is not the
    member's byte length within its own tar. For clump and single members the
    two coincide; for a part, recover the part's own length from the same
    deterministic split used to build it - see docs/backup-plan/HANDOFF.md's
    "traps" section. Do not store the whole-file size here: step 7's restore
    reads this value as a byte count."""
    if member.part is None:
        return member.size_bytes
    part_sizes = split_part_sizes(member.size_bytes, max_size=max_size_bytes, **_split_args(encrypted, max_size_bytes))
    return part_sizes[member.part - 1]


# Reason recorded for a file whose content is already in the archive (see
# _adopt_existing_copies); like "unchanged" it is not a problem with the run.
ALREADY_BACKED_UP = "already_backed_up"


def _adopt_existing_copies(
    db,
    candidates: list[_Candidate],
    destination: StorageLocation,
    *,
    encrypted: bool,
    key_version_id: int | None,
    master_key: bytes | None = None,
    known_object_keys: set[str] | None = None,
) -> tuple[list[_Candidate], list[dict]]:
    """Files whose exact content is already stored at this archive - a
    renamed or moved file, a second copy, or one that was only touched - are
    recorded against the existing stored copy instead of being uploaded again.
    Skip-unchanged (section 6.1) only recognises the *same path* with the same
    size and mtime; this is what stops a rename from re-uploading the file.

    Only an existing copy that matches how this run would store the file is
    reused: the same encrypted-or-not, and (if encrypted) the current key
    version - so turning encryption on, or rotating the key, is never quietly
    bypassed. Its archives must also be complete (indexed). Returns the
    candidates still needing upload, and skip records for the adopted ones.

    Matching is by content_id (a keyed HMAC, see encryption.derive_content_id
    and docs/backup-plan/DEVIATIONS.md), via the indexed BackupRecordContentId
    table, whenever this run is encrypted (there's a real key_version_id to
    look under) - never by the real sha256, since that would defeat the whole
    point of keying the identifier. An unencrypted run has no key version to
    tag a content_id with, so it falls back to matching on BackupRecord.sha256
    as before this feature existed - that column stays DB-only either way,
    never written to a GCS object.

    Nothing is written to the bucket, so the archive's index file still lists
    only the paths that were present when the content was first uploaded; the
    database (via BackupRecordContentId, or the sha256 column for a plain
    run) knows the rest."""
    if not candidates:
        return candidates, []

    use_content_id = encrypted and key_version_id is not None and master_key is not None
    content_id_by_sha: dict[str, str] = {}
    source_by_key: dict[str, BackupRecord] = {}

    if use_content_id:
        content_id_by_sha = {
            c.file_to_pack.sha256: derive_content_id(master_key, c.file_to_pack.sha256) for c in candidates
        }
        content_ids = sorted(set(content_id_by_sha.values()))
        record_id_by_content_id: dict[str, int] = {}
        for i in range(0, len(content_ids), 500):
            for row in db.query(BackupRecordContentId).filter(
                BackupRecordContentId.key_version_id == key_version_id,
                BackupRecordContentId.content_id.in_(content_ids[i : i + 500]),
            ):
                record_id_by_content_id.setdefault(row.content_id, row.backup_record_id)
        record_ids = sorted(set(record_id_by_content_id.values()))
        records_by_id: dict[int, BackupRecord] = {}
        for i in range(0, len(record_ids), 500):
            for record in db.query(BackupRecord).filter(
                BackupRecord.id.in_(record_ids[i : i + 500]),
                BackupRecord.destination_storage_location_id == destination.id,
                BackupRecord.status == "done",
            ):
                records_by_id[record.id] = record
        for content_id, record_id in record_id_by_content_id.items():
            record = records_by_id.get(record_id)
            if record is not None:
                source_by_key[content_id] = record
    else:
        hashes = sorted({c.file_to_pack.sha256 for c in candidates})
        for i in range(0, len(hashes), 500):
            for record in (
                db.query(BackupRecord)
                .filter(
                    BackupRecord.destination_storage_location_id == destination.id,
                    BackupRecord.status == "done",
                    BackupRecord.sha256.in_(hashes[i : i + 500]),
                )
                .order_by(BackupRecord.id)
            ):
                source_by_key.setdefault(record.sha256, record)

    if not source_by_key:
        return candidates, []

    links_by_record: dict[int, list[tuple[BackupRecordArchive, BackupArchive]]] = {}
    source_ids = [r.id for r in source_by_key.values()]
    for i in range(0, len(source_ids), 500):
        for link, archive in (
            db.query(BackupRecordArchive, BackupArchive)
            .join(BackupArchive, BackupArchive.id == BackupRecordArchive.backup_archive_id)
            .filter(BackupRecordArchive.backup_record_id.in_(source_ids[i : i + 500]))
            .order_by(BackupRecordArchive.part_index)
        ):
            links_by_record.setdefault(link.backup_record_id, []).append((link, archive))

    def usable(record: BackupRecord) -> bool:
        links = links_by_record.get(record.id)
        return bool(links) and all(
            a.archive_id is not None
            and a.indexed_at is not None
            and bool(a.encrypted) == encrypted
            and (not encrypted or a.key_version_id == key_version_id)
            and (known_object_keys is None or a.path in known_object_keys)
            for _, a in links
        )

    remaining: list[_Candidate] = []
    adopted: list[dict] = []
    for c in candidates:
        key = content_id_by_sha[c.file_to_pack.sha256] if use_content_id else c.file_to_pack.sha256
        source = source_by_key.get(key)
        if source is None or not usable(source):
            remaining.append(c)
            continue
        media_file = c.media_file
        record = (
            source
            if source.media_file_id == media_file.id
            else db.query(BackupRecord)
            .filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id)
            .one_or_none()
        )
        if record is None:
            record = BackupRecord(media_file_id=media_file.id, destination_storage_location_id=destination.id)
            db.add(record)
        record.local_path = media_file.path
        record.local_checksum = media_file.fingerprint
        record.sha256 = c.file_to_pack.sha256
        record.mtime_ns = c.file_to_pack.mtime_ns
        record.compression = source.compression
        record.status = "done"
        record.verify_status = None
        record.verified_at = None
        db.flush()
        if use_content_id:
            _upsert_content_id(db, record, key_version_id, key)
        if record.id != source.id:
            db.query(BackupRecordArchive).filter_by(backup_record_id=record.id).delete()
            for link, archive in links_by_record[source.id]:
                db.add(
                    BackupRecordArchive(
                        backup_record_id=record.id,
                        backup_archive_id=archive.id,
                        part_index=link.part_index,
                        archive_offset=link.archive_offset,
                        archive_length=link.archive_length,
                    )
                )
        adopted.append({"path": media_file.path, "reason": ALREADY_BACKED_UP})
    db.commit()
    return remaining, adopted


def _record_archive(
    db,
    archive: PackedArchive,
    *,
    object_key: str,
    crc32c: str,
    destination: StorageLocation,
    max_size_bytes: int,
    rel_path_to_media_file: dict[str, MediaFile],
    cleared_record_ids: set[int],
    encrypted: bool = False,
    rel_path_to_mtime_ns: dict[str, int] | None = None,
    key_version_id: int | None = None,
) -> None:
    """Section 6 step 6, for one archive: one BackupArchive row, then one
    BackupRecord + BackupRecordArchive per cataloged file the archive
    contains. Committed as one transaction, only ever called after that
    archive's index file is confirmed written.

    `cleared_record_ids` is shared across every archive in the run: a split
    file's parts each live in their own archive and each call this function
    once, so a record's *old* BackupRecordArchive rows (from a previous run)
    must be cleared exactly once per run, the first time this run touches
    that record - clearing them again on the second part would delete the
    first part's link this same run just wrote.
    """
    backup_archive = BackupArchive(
        storage_location_id=destination.id,
        path=object_key,
        size_bytes=archive.size_bytes,
        encrypted=encrypted,
        archive_id=archive.archive_id,
        archive_type=archive.archive_type,
        crc32c=crc32c,
        indexed_at=datetime.now(timezone.utc),
        key_version_id=key_version_id if encrypted else None,
    )
    db.add(backup_archive)
    db.flush()  # need backup_archive.id before linking

    for member in archive.members:
        archive_length = _archive_length_for_member(member, max_size_bytes=max_size_bytes, encrypted=encrypted)
        part_index = 0 if member.part is None else member.part - 1

        for rel_path in member.paths:
            media_file = rel_path_to_media_file[rel_path]

            record = (
                db.query(BackupRecord)
                .filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id)
                .one_or_none()
            )
            if not record:
                record = BackupRecord(
                    media_file_id=media_file.id, destination_storage_location_id=destination.id
                )
                db.add(record)
            record.local_path = media_file.path
            # Keep writing the legacy partial-BLAKE2b fingerprint too, so the
            # existing drift comparison (unrelated to this pipeline) keeps
            # working - see docs/backup-plan/steps/06-backup-run.md.
            record.local_checksum = media_file.fingerprint
            record.sha256 = member.sha256
            record.compression = member.compression
            # The mtime this path's file was actually hashed at, not the
            # catalog's and not the group representative's: duplicate-content
            # files share one member (whose mtime is the representative's), so
            # using member.mtime_ns would make every other copy look "changed"
            # on the next run and get re-uploaded forever.
            record.mtime_ns = (rel_path_to_mtime_ns or {}).get(rel_path, member.mtime_ns)
            record.status = "done"
            record.verify_status = None
            record.verified_at = None
            db.flush()  # need record.id before linking

            if encrypted and key_version_id is not None and member.content_id is not None:
                _upsert_content_id(db, record, key_version_id, member.content_id)

            if record.id not in cleared_record_ids:
                db.query(BackupRecordArchive).filter_by(backup_record_id=record.id).delete()
                cleared_record_ids.add(record.id)
            db.add(
                BackupRecordArchive(
                    backup_record_id=record.id,
                    backup_archive_id=backup_archive.id,
                    part_index=part_index,
                    archive_offset=member.offset,
                    archive_length=archive_length,
                )
            )

    db.commit()


def _execute_backup_run(
    source_storage_location_id: int,
    destination_storage_location_id: int,
    *,
    file_ids: list[int] | None = None,
    run_id: int | None = None,
    backend: StorageBackend | None = None,
    upload_sleep=time.sleep,
) -> dict:
    db = SessionLocal()
    try:
        try:
            return _execute_backup_run_inner(
                db,
                source_storage_location_id,
                destination_storage_location_id,
                file_ids=file_ids,
                run_id=run_id,
                backend=backend,
                upload_sleep=upload_sleep,
            )
        except Exception as exc:
            # Record why, so the UI shows the reason instead of a run stuck
            # at "queued"/"running"; still raise so callers/tests see it.
            db.rollback()
            error_message = (str(exc) or exc.__class__.__name__)[:2000]
            _update_run(
                db,
                run_id,
                status="failed",
                error_message=error_message,
                detail=None,
                completed_at=datetime.now(timezone.utc),
            )
            # Cancelled-by-user runs (see main.py's _cancel_run) never reach
            # here - they're revoked/terminated, not raised - so this only
            # fires for genuine failures.
            source_location = db.get(StorageLocation, source_storage_location_id)
            notify(
                db,
                "backup_failed",
                "error",
                f"Backup failed: {source_location.name if source_location else source_storage_location_id}",
                f"Backup of \"{source_location.name if source_location else source_storage_location_id}\" failed: {error_message}",
            )
            raise
    finally:
        db.close()


def _execute_backup_run_inner(
    db,
    source_storage_location_id: int,
    destination_storage_location_id: int,
    *,
    file_ids: list[int] | None,
    run_id: int | None,
    backend: StorageBackend | None,
    upload_sleep,
) -> dict:
    source_location = db.get(StorageLocation, source_storage_location_id)
    destination = db.get(StorageLocation, destination_storage_location_id)

    _refuse_if_unusable(db, destination)
    if source_location is None:
        raise BackupRunRefused("source storage location does not exist")

    # Fixed pre-existing bug: this used to build a GCSBackend directly here
    # instead of going through _backend_for_destination, so an S3 archive
    # would fail (or silently target the wrong provider) at this call site
    # even though _backend_for_destination itself already supported S3. Now
    # delegates to it (which in turn delegates to storage.backend_for_destination)
    # so GCS and S3 destinations both work here.
    cloud_config = db.query(CloudStorageConfig).first()

    # TransferConfig is a singleton row created lazily by the Settings
    # page's first save (see web/app/main.py) - a fresh install with
    # nothing saved yet has no row at all. Falling back to `TransferConfig()`
    # would not work here: mapped_column(default=...) only applies at
    # flush/INSERT time, so an unpersisted instance's fields are all None.
    transfer_config = db.query(TransferConfig).first() or TransferConfig(
        min_size_bytes=10485760, clump_size_bytes=67108864, max_size_bytes=1073741824
    )

    if source_location is not None:
        bucket, prefix = _resolve_target(destination, source_location, cloud_config)
    else:
        bucket, prefix = (cloud_config.bucket_name if cloud_config else None), ""

    if backend is None:
        backend = _backend_for_destination(db, destination, storage_class=destination.storage_class)

    # A dedicated connection, held for the whole run - not the ORM
    # session's. Advisory locks are session (connection) scoped: if
    # SQLAlchemy returned the session's connection to the pool mid-run,
    # the lock would leak onto a pooled connection and every later run
    # would block until the worker restarted.
    lock_conn = engine.connect()
    try:
        got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": BACKUP_RUN_LOCK_KEY}).scalar()
        if not got:
            _update_run(db, run_id, detail="Waiting for another backup run to finish")
            return {"status": "skipped", "reason": "another run holds the lock"}

        try:
            _update_run(db, run_id, status="running", started_at=datetime.now(timezone.utc), detail="Scanning")
            return _run_locked(
                db,
                source_location=source_location,
                destination=destination,
                cloud_config=cloud_config,
                transfer_config=transfer_config,
                backend=backend,
                upload_sleep=upload_sleep,
                file_ids=file_ids,
                run_id=run_id,
                prefix=prefix,
            )
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BACKUP_RUN_LOCK_KEY})
    finally:
        lock_conn.close()


def _run_locked(
    db,
    *,
    source_location: StorageLocation,
    destination: StorageLocation,
    cloud_config: CloudStorageConfig | None,
    transfer_config: TransferConfig,
    backend: StorageBackend,
    upload_sleep=time.sleep,
    file_ids: list[int] | None = None,
    run_id: int | None = None,
    prefix: str | None = None,
) -> dict:
    if prefix is None:
        prefix = normalize_prefix(cloud_config.prefix if cloud_config else None)
    progress = _Progress(db, run_id)
    # One batched bucket listing for the whole run (same cost shape as
    # _VerifyContext.prime_stats in tasks.py: ~1 request per 1000 objects,
    # metadata only) so skip-unchanged and adopt-existing-copy never trust a
    # BackupRecord/BackupArchive row whose actual object was deleted
    # out-of-band (e.g. directly from the bucket) - see docs/CHANGES.md.
    known_object_keys: set[str] = set(backend.list_stats(prefix).keys())
    run = db.get(BackupRun, run_id) if run_id is not None else None
    force_all = bool(run and run.mode == "replace_all")
    candidates, skipped = _build_file_list(
        db,
        source_location,
        destination.id,
        file_ids,
        progress=progress,
        known_object_keys=known_object_keys,
        force_all=force_all,
    )

    master_key = _get_master_key(db)
    # A library can opt OUT of the global encryption setting
    # (source_location.encrypted=False) but can't opt IN without a master
    # key actually configured - there's one passphrase for the whole app,
    # not a per-library key, so "encrypt me" with no key configured yet is a
    # no-op rather than an error (StorageLocation.encrypted docstring).
    encrypted = master_key is not None and source_location.encrypted is not False
    key_version_id = _current_key_version_id(db) if encrypted else None
    sha256_seal_key = derive_sha256_seal_key(master_key) if encrypted else None
    key_check = derive_key_check(master_key) if encrypted else None
    candidates, adopted = _adopt_existing_copies(
        db,
        candidates,
        destination,
        encrypted=encrypted,
        key_version_id=key_version_id,
        master_key=master_key,
        known_object_keys=known_object_keys,
    )
    skipped += adopted
    rel_path_to_media_file = {c.file_to_pack.rel_path: c.media_file for c in candidates}
    rel_path_to_mtime_ns = {c.file_to_pack.rel_path: c.file_to_pack.mtime_ns for c in candidates}
    # section 6.2: a file that changes mid-read or disappears is skipped and
    # logged, with no retry in v2.0; that alone makes the run "partial". An
    # excluded file (deliberate config), an unchanged file (section 6.1, the
    # expected common case), or a file already known gone as of the last
    # scan (already_missing - discovered before this run, not by it) are not
    # problems with the run.
    partial = any(s["reason"] not in ("excluded", "unchanged", ALREADY_BACKED_UP, "already_missing") for s in skipped)

    bytes_uploaded = 0
    archives_recorded = 0
    cleared_record_ids: set[int] = set()

    # What "files_total" actually means for a done/partial run: every file
    # here gets packed and uploaded this run (adopted existing-content copies
    # already moved into `skipped` above, with their own reason).
    backed_up = [{"path": c.media_file.path, "size_bytes": c.file_to_pack.size_bytes} for c in candidates]

    _update_run(
        db,
        run_id,
        files_total=len(candidates),
        files_skipped=len(skipped),
        skipped_json=json.dumps(skipped),
        backed_up_json=json.dumps(backed_up),
        encrypted=encrypted,
        detail=(
            ("Compressing, " if getattr(transfer_config, "compression_enabled", False) else "")
            + ("encrypting and packing" if encrypted else "packing")
        ).capitalize()
        if candidates
        else None,
    )

    compression_level = (
        min(9, max(1, transfer_config.compression_level or DEFAULT_LEVEL))
        if getattr(transfer_config, "compression_enabled", False)
        else None
    )

    with tempfile.TemporaryDirectory(prefix="mb-backup-") as tmp_dir:
        prepared_dir = Path(tmp_dir) / "prepared"
        if encrypted or compression_level is not None:
            # Compress and/or encrypt every file first (step 3), then
            # clump/split the result (steps 4-5). The prepared files live in
            # their own dir, dropped as soon as the tars are written, so peak
            # disk is one extra copy of the run's (compressed) data.
            to_pack = _prepare_candidates(
                candidates,
                prepared_dir,
                master_key=master_key,
                chunk_size=_blob_chunk_size(transfer_config.max_size_bytes),
                compression_level=compression_level,
                content_id_key=master_key if encrypted else None,
                progress=progress,
            )
        else:
            to_pack = [c.file_to_pack for c in candidates]
        progress.phase("packing")
        archives = pack(
            to_pack,
            dest_dir=Path(tmp_dir),
            min_size=transfer_config.min_size_bytes,
            clump_size=transfer_config.clump_size_bytes,
            max_size=transfer_config.max_size_bytes,
            **_split_args(encrypted, transfer_config.max_size_bytes),
        )
        shutil.rmtree(prepared_dir, ignore_errors=True)
        _update_run(db, run_id, archives_total=len(archives), detail=f"Uploading 0/{len(archives)}" if archives else None)
        progress.phase("uploading", sum(a.size_bytes for a in archives))

        # A hard cap on upload speed - transfer_config.max_upload_mbps if the
        # user set one (Settings > Upload Bandwidth), else half of the last
        # measured speed, else a conservative default before any speed test
        # has run (gcs.effective_upload_mbps; keep in sync with web's copy).
        # GCSBackend and S3Backend both enforce this (S3Backend wraps the same
        # gcs._ThrottledReader). cloud_config may be None for an archive
        # backed entirely by S3 with no CloudStorageConfig row at all, in
        # which case there's no measured-speed reading to fall back on.
        max_bytes_per_sec = gcs.upload_mbps_to_throttle_bytes_per_sec(
            cloud_config.upload_mbps if cloud_config else None, transfer_config.max_upload_mbps
        )

        for archive_number, archive in enumerate(archives, 1):
            object_key = archive_object_key(archive.archive_id, prefix)
            idx_key = index_object_key(archive.archive_id, prefix)

            # Section 6 steps 4-5, in this order and no other: confirm the
            # upload before writing the index. An archive with no index file
            # is incomplete and restore ignores it (section 6, last line) -
            # if upload_and_confirm exhausts its retries it raises, and we
            # deliberately let that fail the whole task rather than
            # downgrading to "partial": no index and no rows get written for
            # this (or any later) archive, which is the section 6 guarantee.
            # upload_and_confirm checksums the whole archive before sending
            # anything, which for a big archive is a visible pause - label it.
            _update_run(db, run_id, detail=f"Checksumming {archive_number}/{len(archives)}")
            stat = upload_and_confirm(
                backend,
                object_key,
                archive.local_path,
                sleep=upload_sleep,
                max_bytes_per_sec=max_bytes_per_sec,
                progress_cb=lambda sent, base=bytes_uploaded: progress.advance(base + sent),
                on_checksummed=lambda n=archive_number: _update_run(
                    db, run_id, detail=f"Uploading {n}/{len(archives)}"
                ),
            )
            index = build_index(
                archive,
                prefix=prefix,
                created_at=datetime.now(timezone.utc),
                path_encryptor=(
                    (lambda sha, paths: encrypt_paths(derive_file_key(master_key, sha), sha, paths))
                    if encrypted
                    else None
                ),
                sha256_encryptor=(
                    (lambda content_id, sha, _seal_key=sha256_seal_key: encrypt_sha256(_seal_key, content_id, sha))
                    if encrypted
                    else None
                ),
                key_check=key_check,
            )
            backend.write_bytes(idx_key, json.dumps(index).encode())

            # Section 6 step 6: the archive and its files, one transaction.
            _record_archive(
                db,
                archive,
                object_key=object_key,
                crc32c=stat.crc32c,
                destination=destination,
                max_size_bytes=transfer_config.max_size_bytes,
                rel_path_to_media_file=rel_path_to_media_file,
                cleared_record_ids=cleared_record_ids,
                encrypted=encrypted,
                rel_path_to_mtime_ns=rel_path_to_mtime_ns,
                key_version_id=key_version_id,
            )
            archives_recorded += 1
            bytes_uploaded += stat.size
            progress.advance(bytes_uploaded)
            progress.flush()
            _update_run(db, run_id, archives_done=archives_recorded, bytes_uploaded=bytes_uploaded)

            # Section 6 step 7: delete the temp file once it's uploaded,
            # indexed, and recorded - disk use stays bounded by one archive
            # at a time instead of the whole run.
            archive.local_path.unlink()

    progress.finish()
    final_status = "partial" if partial else "done"
    _update_run(
        db,
        run_id,
        status=final_status,
        detail=None,
        completed_at=datetime.now(timezone.utc),
    )
    notify(
        db,
        "backup_failed" if final_status == "partial" else "backup_done",
        "warning" if final_status == "partial" else "info",
        f"Backup {final_status}: {source_location.name}",
        f"Backup of \"{source_location.name}\" to \"{destination.name}\" finished {final_status}: "
        f"{archives_recorded} archive(s), {len(candidates)} file(s) packed"
        + (f", {len(skipped)} skipped" if skipped else "") + ".",
    )
    return {
        "status": "partial" if partial else "ok",
        "archives": archives_recorded,
        "files": len(candidates),
        "skipped": skipped,
        "adopted": len(adopted),
        "bytes_uploaded": bytes_uploaded,
    }


@app.task(bind=True, name="run_backup")
def run_backup(
    self,
    source_storage_location_id: int,
    destination_storage_location_id: int,
    file_ids: list[int] | None = None,
    run_id: int | None = None,
) -> dict:
    """file_ids limits the run to those catalog files (single-file / selected
    backups); None means the whole library. run_id is the BackupRun row the UI
    tracks. If another run holds the advisory lock this run stays "queued" and
    retries every 15s, so overlapping clicks line up instead of failing."""
    if run_id is not None:
        db = SessionLocal()
        try:
            _update_run(db, run_id, celery_task_id=self.request.id)
        finally:
            db.close()
    result = _execute_backup_run(
        source_storage_location_id, destination_storage_location_id, file_ids=file_ids, run_id=run_id
    )
    if result.get("status") == "skipped":
        raise self.retry(countdown=15, max_retries=480)
    return result
