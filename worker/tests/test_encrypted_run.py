"""End-to-end tests for the encrypted v2 pipeline (docs/backup-plan/steps/10-encryption.md):
run_backup encrypts each file, clumps/splits the ciphertext, seals paths in
the index, and restore decrypts and verifies. Database-backed
(MEDIABRIDGE_TEST_DB=1); never touches a real bucket."""
from __future__ import annotations

import json
import os

import pytest

from app import backup_run
from app.backup_run import _execute_backup_run
import hashlib

from app.encryption import decrypt_paths, decrypt_sha256, derive_file_key, derive_master_key, derive_sha256_seal_key
from app.models import BackupArchive
from app.tasks import _restore_v2, _v2_ledger_parts
from tests.conftest import (
    set_cloud_config,
    set_encryption_key,
    set_transfer_config,
    usable_destination,
    write_file,
)
from tests.storage_double import LocalBackend

pytestmark = pytest.mark.usefixtures(
    "encryption_config_state", "cloud_storage_config_state", "transfer_config_state"
)

CHUNK = 4096


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    # Tiny encryption chunks so a few-MiB file spans many chunks and parts.
    monkeypatch.setattr(backup_run, "BLOB_CHUNK_SIZE", CHUNK)


def _run(db_session, catalog, tmp_path, files: dict[str, bytes], **transfer):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    set_transfer_config(db_session, **transfer)
    set_cloud_config(db_session)
    set_encryption_key(db_session)
    media_files = {}
    for name, data in files.items():
        write_file(tmp_path, name, data)
        media_files[name] = catalog.make_media_file(
            path=str(tmp_path / name), filename=name, storage_location_id=source.id, size_bytes=len(data)
        )
    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"
    return destination, media_files, backend


def _restore(db_session, media_file, destination, backend, out):
    record, parts = _v2_ledger_parts(db_session, media_file.id, destination.id)
    _restore_v2(db_session, media_file, record, parts, out, backend)


def test_clump_single_and_split_round_trip_encrypted(db_session, catalog, tmp_path):
    files = {
        "tiny1.txt": b"secret one",
        "tiny2.txt": b"secret two",
        "mid.bin": os.urandom(30_000),
        "big.bin": os.urandom(3 * 1024 * 1024 + 123),
    }
    destination, mfs, backend = _run(
        db_session,
        catalog,
        tmp_path,
        files,
        min_size_bytes=1000,
        clump_size_bytes=100_000,
        max_size_bytes=2 * 1024 * 1024,
    )

    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).all()
    assert {a.archive_type for a in archives} == {"clump", "single", "part"}
    assert all(a.encrypted for a in archives)
    assert all(a.size_bytes <= 2 * 1024 * 1024 for a in archives)

    for name, data in files.items():
        out = tmp_path / "restored" / name
        _restore(db_session, mfs[name], destination, backend, out)
        assert out.read_bytes() == data


def test_bucket_holds_no_plaintext_names_or_content(db_session, catalog, tmp_path):
    files = {"Holiday Photos/img001.jpg": b"pixels-" * 100, "notes.txt": b"my private notes"}
    (tmp_path / "Holiday Photos").mkdir()
    destination, mfs, backend = _run(
        db_session, catalog, tmp_path, files, min_size_bytes=100, clump_size_bytes=100_000, max_size_bytes=2 * 1024 * 1024
    )
    real_shas = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}

    seen = 0
    for obj in (tmp_path / "bucket").rglob("*"):
        if not obj.is_file():
            continue
        seen += 1
        blob = obj.read_bytes()
        assert b"Holiday" not in blob and b"img001" not in blob and b"notes.txt" not in blob
        assert b"my private notes" not in blob and b"pixels-" not in blob
        # D14: the real sha256 must never appear in any bucket object either -
        # it's the whole point of keying entries by content_id instead.
        for sha in real_shas.values():
            assert sha.encode() not in blob

    assert seen

    # ...but the sealed paths (and, since D14, the sealed real sha256) in the
    # index open with the global key.
    master = derive_master_key("test-global-key", bytes.fromhex("00112233445566778899aabbccddeeff"))
    seal_key = derive_sha256_seal_key(master)
    recovered = set()
    for idx in (tmp_path / "bucket").rglob("*.json"):
        doc = json.loads(idx.read_text())
        assert doc["encrypted"] is True
        assert doc["key_scheme"] == "content_id_v1"
        for content_id, entry in doc["files"].items():
            assert "paths" not in entry
            sha = decrypt_sha256(seal_key, content_id, entry["sha256_enc"])
            assert sha in real_shas.values()
            recovered.update(decrypt_paths(derive_file_key(master, sha), sha, entry["paths_enc"]))
    assert recovered == {"Holiday Photos/img001.jpg", "notes.txt"}


def test_restore_uses_recorded_key_version_after_rotation(db_session, catalog, tmp_path):
    from app.models import BackupArchive, BackupKeyVersion

    data = os.urandom(20_000)
    destination, mfs, backend = _run(
        db_session, catalog, tmp_path, {"a.bin": data}, min_size_bytes=100, clump_size_bytes=100_000, max_size_bytes=2 * 1024 * 1024
    )
    archive = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).one()
    assert archive.key_version_id is not None  # the run recorded which key it used

    # Rotate: the current key is now something else, the old one is a retired version.
    set_encryption_key(db_session, password="a-different-key")
    db_session.get(BackupKeyVersion, archive.key_version_id).retired_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    db_session.commit()

    out = tmp_path / "restored" / "a.bin"
    _restore(db_session, mfs["a.bin"], destination, backend, out)
    assert out.read_bytes() == data


def test_restore_fails_with_wrong_key_when_no_version_recorded(db_session, catalog, tmp_path):
    from app.models import BackupArchive

    data = os.urandom(20_000)
    destination, mfs, backend = _run(
        db_session, catalog, tmp_path, {"a.bin": data}, min_size_bytes=100, clump_size_bytes=100_000, max_size_bytes=2 * 1024 * 1024
    )
    # An archive with no recorded version falls back to the current key.
    db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).update({"key_version_id": None})
    db_session.commit()
    set_encryption_key(db_session, password="a-different-key")

    out = tmp_path / "restored" / "a.bin"
    with pytest.raises(RuntimeError, match="decryption"):
        _restore(db_session, mfs["a.bin"], destination, backend, out)
    assert not out.exists()
