"""Web-side key verification for the portability feature
(docs/backup-plan/steps/18-portability.md, DEVIATIONS.md D13).

`discover-bucket` lists bucket objects but historically never tried to
decrypt anything, so it couldn't tell the user whether the master key
they've configured actually opens content found in someone else's (or an
old) archive folder - that only happened inside the Celery `sync_run` task,
after the user had already committed to syncing. This module lets the web
process itself try a cheap decrypt (index files only, no tar payload reads)
so the Libraries page can warn *before* the user clicks sync.

Per the established web/worker split (each side duplicates the small helpers
it needs - see gcs.py/fingerprint.py), this mirrors the pieces of
worker/app/encryption.py and worker/app/sync_run.py it needs, against
web/app/models.py, without importing worker/Celery code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Callable, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.models import BackupEncryptionConfig, BackupKeyVersion

KDF_ITERATIONS = 200_000


def derive_master_key(password: str, kdf_salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), kdf_salt, KDF_ITERATIONS, dklen=32)


def derive_file_key(master_key: bytes, file_uuid: str) -> bytes:
    return hmac.new(master_key, b"mediabridge-backup-file-key:" + file_uuid.encode(), hashlib.sha256).digest()


def derive_content_id(master_key: bytes, sha256: str) -> str:
    """Mirrors worker/app/encryption.derive_content_id - see its docstring.
    Needed here (not just in worker) so key_versions.py can backfill
    BackupRecordContentId for a newly-rotated key without importing Celery
    code - see docs/backup-plan/DEVIATIONS.md."""
    return hmac.new(master_key, b"mediabridge-content-id:" + sha256.encode(), hashlib.sha256).hexdigest()


def derive_key_check(master_key: bytes) -> str:
    """Mirrors worker/app/encryption.derive_key_check - see its docstring.
    Lets check_key_match confirm a candidate password against an archive's
    stored "key_check" directly, without trial-decrypting a real entry."""
    return hmac.new(master_key, b"mediabridge-key-check", hashlib.sha256).hexdigest()[:16]


def decrypt_paths(file_key: bytes, sha256: str, token: str) -> list[str]:
    raw = base64.b64decode(token)
    return json.loads(AESGCM(file_key).decrypt(raw[:12], raw[12:], sha256.encode()))


def derive_sha256_seal_key(master_key: bytes) -> bytes:
    """Mirrors worker/app/encryption.derive_sha256_seal_key - see its
    docstring. Needed here because a content-id-keyed index's "files" dict
    key is no longer the real sha256 (docs/backup-plan/DEVIATIONS.md); this
    is what recovers it from an entry's sealed "sha256_enc"."""
    return hmac.new(master_key, b"mediabridge-sha256-seal-key", hashlib.sha256).digest()


def decrypt_sha256(seal_key: bytes, content_id: str, token: str) -> str:
    raw = base64.b64decode(token)
    return AESGCM(seal_key).decrypt(raw[:12], raw[12:], content_id.encode()).decode()


def all_candidate_keys(db) -> list[tuple[Optional[int], bytes]]:
    """Every master key this instance knows how to derive, current key
    first: (key_version_id, master_key). Same logic as
    worker/app/sync_run.py:_all_candidate_keys, against web's models."""
    keys: list[tuple[Optional[int], bytes]] = []
    seen: set[tuple[str, str]] = set()
    config = db.query(BackupEncryptionConfig).first()
    if config and config.password and config.kdf_salt:
        current_id = None
        current_row = (
            db.query(BackupKeyVersion)
            .filter_by(password=config.password, kdf_salt=config.kdf_salt, retired_at=None)
            .order_by(BackupKeyVersion.id.desc())
            .first()
        )
        if current_row:
            current_id = current_row.id
        keys.append((current_id, derive_master_key(config.password, bytes.fromhex(config.kdf_salt))))
        seen.add((config.password, config.kdf_salt))
    for row in db.query(BackupKeyVersion):
        if (row.password, row.kdf_salt) in seen:
            continue
        seen.add((row.password, row.kdf_salt))
        keys.append((row.id, derive_master_key(row.password, bytes.fromhex(row.kdf_salt))))
    return keys


# check_key_match's possible results:
#   "match"       - every encrypted entry seen decrypted with a candidate key
#   "no_match"    - encrypted entries were found but none decrypted
#   "mixed"       - some archives decrypted, others didn't (partial rotation,
#                   or content from more than one source)
#   "unencrypted" - index files were found but none were encrypted
#   "no_content"  - no (well-formed) index files were found at all


def check_key_match(
    index_keys: list[str],
    read_object: Callable[[str], Optional[bytes]],
    candidate_keys: list[tuple[Optional[int], bytes]],
) -> str:
    """Reads each index.json at `index_keys` (cheap: small plaintext/base64
    JSON, no tar payload reads - same cost profile bucket_inventory.discover
    already relies on) and tries every encrypted entry against
    `candidate_keys`, caching the result per archive_id like
    sync_run._PathResolver (one archive's worth of files costs at most
    len(candidate_keys) decrypt attempts, not one per file)."""
    key_for_archive: dict[str, tuple[Optional[int], bytes] | None] = {}
    saw_encrypted = False
    saw_plaintext = False
    any_matched = False
    any_unmatched = False

    for index_key in index_keys:
        raw = read_object(index_key)
        if raw is None:
            continue
        try:
            index = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(index, dict) or not isinstance(index.get("files"), dict):
            continue
        archive_id = index.get("archive_id") or index_key
        content_id_keyed = index.get("key_scheme") == "content_id_v1"
        key_check = index.get("key_check")
        if key_check is not None and archive_id not in key_for_archive:
            saw_encrypted = True
            matched_key = next(
                (
                    (key_version_id, master_key)
                    for key_version_id, master_key in candidate_keys
                    if derive_key_check(master_key) == key_check
                ),
                None,
            )
            key_for_archive[archive_id] = matched_key
            if matched_key is not None:
                any_matched = True
            else:
                any_unmatched = True
            continue
        for key, entry in index["files"].items():
            if not isinstance(entry, dict):
                continue
            if content_id_keyed:
                # A content-id-keyed entry's dict key is a keyed HMAC, not
                # the real sha256 - "sha256_enc" is what a matching key
                # actually opens here (paths_enc's AAD is the real sha256,
                # which we don't have without decrypting this first anyway).
                token = entry.get("sha256_enc")
                decrypt = lambda master_key, token=token, key=key: decrypt_sha256(
                    derive_sha256_seal_key(master_key), key, token
                )
            elif "paths" in entry:
                saw_plaintext = True
                continue
            else:
                token = entry.get("paths_enc")
                decrypt = lambda master_key, token=token, key=key: decrypt_paths(
                    derive_file_key(master_key, key), key, token
                )
            if not token:
                continue
            saw_encrypted = True
            if archive_id in key_for_archive:
                cached = key_for_archive[archive_id]
                if cached is not None:
                    any_matched = True
                else:
                    any_unmatched = True
                continue
            matched = False
            for key_version_id, master_key in candidate_keys:
                try:
                    decrypt(master_key)
                except (InvalidTag, ValueError):
                    continue
                key_for_archive[archive_id] = (key_version_id, master_key)
                any_matched = True
                matched = True
                break
            if not matched:
                key_for_archive[archive_id] = None
                any_unmatched = True

    if not saw_encrypted and not saw_plaintext:
        return "no_content"
    if not saw_encrypted:
        return "unencrypted"
    if any_matched and any_unmatched:
        return "mixed"
    if any_matched:
        return "match"
    return "no_match"
