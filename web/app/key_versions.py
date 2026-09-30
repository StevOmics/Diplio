"""Backup key history (docs/backup-plan/steps/12-key-versions.md).

BackupEncryptionConfig holds the *current* passphrase (what the worker reads);
BackupKeyVersion keeps every passphrase ever used, so changing the key never
loses the old one. The current key always has exactly one row with
retired_at NULL.
"""
import secrets
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.encryption_check import derive_content_id, derive_master_key
from app.models import BackupArchive, BackupEncryptionConfig, BackupKeyVersion, BackupRecord, BackupRecordContentId


def ensure_current_version(db: Session, config: BackupEncryptionConfig) -> BackupKeyVersion | None:
    """The version row for config's current password/salt, creating it if
    missing (installs that set a key before versions existed). created_at for
    a backfilled row is the config's last update - the best date available."""
    if not config or not config.password or not config.kdf_salt:
        return None
    current = (
        db.query(BackupKeyVersion)
        .filter_by(password=config.password, kdf_salt=config.kdf_salt, retired_at=None)
        .order_by(BackupKeyVersion.id.desc())
        .first()
    )
    if current:
        return current
    current = BackupKeyVersion(
        password=config.password,
        kdf_salt=config.kdf_salt,
        created_at=config.updated_at or datetime.now(timezone.utc),
    )
    db.add(current)
    db.flush()
    return current


def kdf_salt_is_valid_hex(salt: str) -> bool:
    """True for a salt shaped like one this app would have generated itself
    (secrets.token_hex(16): 32 lowercase hex chars) - what a user pastes in
    from an old instance's Settings page should look exactly like that."""
    if len(salt) != 32:
        return False
    try:
        bytes.fromhex(salt)
    except ValueError:
        return False
    return True


def backfill_content_ids(db: Session, key_version_id: int, master_key: bytes) -> None:
    """Populates BackupRecordContentId for `key_version_id` across every
    already-archived record, computed purely from data already in the
    database (each record's sha256) - no file re-read, no bucket access.
    This is what lets dedup keep recognizing previously-archived content
    after a key rotation (docs/backup-plan/DEVIATIONS.md), instead of the
    new key version only ever matching content backed up after the rotation.
    Cheap enough to run inline with the rotation itself (one HMAC per
    record); safe to call more than once (skips records it already covered)."""
    already_covered = {
        row.backup_record_id
        for row in db.query(BackupRecordContentId.backup_record_id).filter_by(key_version_id=key_version_id)
    }
    for record in db.query(BackupRecord).filter(BackupRecord.sha256.isnot(None)):
        if record.id in already_covered:
            continue
        db.add(
            BackupRecordContentId(
                backup_record_id=record.id,
                key_version_id=key_version_id,
                content_id=derive_content_id(master_key, record.sha256),
            )
        )


def set_passphrase(db: Session, config: BackupEncryptionConfig, password: str, salt: str | None = None) -> bool:
    """Makes `password` (+ `salt`, if given) the current key, retiring (not
    deleting) the previous one. Returns False and changes nothing if it's
    already the current key. Caller commits.

    `salt` is for portability (see docs/backup-plan/steps/18-portability.md):
    the salt is not secret (see encryption.py's derive_master_key docstring),
    but it IS half of what derives the master key, and a fresh instance
    normally mints a random one - so the exact same password on a new
    instance produces a *different* key unless the old instance's salt is
    carried over too. Passing it here reproduces that exact key instead of
    generating a new one. Caller validates its shape (kdf_salt_is_valid_hex)
    before calling this."""
    if config.password == password and (salt is None or config.kdf_salt == salt):
        return False
    previous = ensure_current_version(db, config)
    now = datetime.now(timezone.utc)
    if previous:
        previous.retired_at = now
    salt = salt or secrets.token_hex(16)
    new_version = BackupKeyVersion(password=password, kdf_salt=salt, created_at=now)
    db.add(new_version)
    db.flush()  # need new_version.id for the backfill below
    backfill_content_ids(db, new_version.id, derive_master_key(password, bytes.fromhex(salt)))
    config.password = password
    config.kdf_salt = salt
    return True


def key_history(db: Session, config: BackupEncryptionConfig | None) -> list[dict]:
    """Newest first, each with its version number (v1 = oldest), whether it's
    current, and how many v2 archives it encrypted."""
    if config and config.password:
        ensure_current_version(db, config)
        db.commit()
    rows = db.query(BackupKeyVersion).order_by(BackupKeyVersion.id).all()
    counts = dict(
        db.query(BackupArchive.key_version_id, func.count(BackupArchive.id))
        .filter(BackupArchive.key_version_id.isnot(None))
        .group_by(BackupArchive.key_version_id)
        .all()
    )
    history = [
        {"number": i, "row": row, "current": row.retired_at is None, "archives": counts.get(row.id, 0)}
        for i, row in enumerate(rows, start=1)
    ]
    return list(reversed(history))
