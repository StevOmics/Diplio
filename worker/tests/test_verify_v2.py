"""Tests for _verify_v2 in worker/app/tasks.py: verifying that a file backed up
by the v2 pipeline really is in the bucket, intact, and still matches the local
file. Database-backed (MEDIABRIDGE_TEST_DB=1, see tests/conftest.py); each test
backs a file up for real through _execute_backup_run against a LocalBackend,
then damages the bucket or the local file and checks the verdict. No test
contacts a real bucket.
"""
from __future__ import annotations

import json

import pytest

from app.backup_index import index_object_key
from app.backup_run import _execute_backup_run
from app.tasks import _v2_ledger_parts, _verify_v2
from tests.conftest import (
    set_cloud_config,
    set_encryption_enabled,
    set_encryption_key,
    set_transfer_config,
    usable_destination,
    write_file,
)
from tests.storage_double import LocalBackend

pytestmark = pytest.mark.usefixtures(
    "encryption_config_state", "cloud_storage_config_state", "transfer_config_state"
)


def _backed_up_file(db_session, catalog, tmp_path, *, encrypted: bool, content: bytes = b"hello world" * 50):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_transfer_config(db_session, min_size_bytes=10, clump_size_bytes=100_000, max_size_bytes=2_000_000)
    if encrypted:
        set_encryption_key(db_session)
    set_encryption_enabled(db_session, encrypted)
    path = write_file(tmp_path, "a.txt", content)
    media_file = catalog.make_media_file(
        path=str(path), filename="a.txt", storage_location_id=source.id, size_bytes=len(content)
    )
    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"
    record, parts = _v2_ledger_parts(db_session, media_file.id, destination.id)
    return media_file, record, parts, backend, path


@pytest.mark.parametrize("encrypted", [False, True])
def test_intact_backup_matching_local_file_is_match(db_session, catalog, tmp_path, encrypted):
    media_file, record, parts, backend, _ = _backed_up_file(db_session, catalog, tmp_path, encrypted=encrypted)
    assert _verify_v2(db_session, media_file, record, parts, backend) == "match"


@pytest.mark.parametrize("encrypted", [False, True])
def test_object_deleted_from_bucket_is_missing(db_session, catalog, tmp_path, encrypted):
    media_file, record, parts, backend, _ = _backed_up_file(db_session, catalog, tmp_path, encrypted=encrypted)
    backend.delete(parts[0][1].path)
    assert _verify_v2(db_session, media_file, record, parts, backend) == "missing"


@pytest.mark.parametrize("encrypted", [False, True])
def test_corrupted_object_is_mismatch(db_session, catalog, tmp_path, encrypted):
    media_file, record, parts, backend, _ = _backed_up_file(db_session, catalog, tmp_path, encrypted=encrypted)
    stored = backend._path(parts[0][1].path)
    data = bytearray(stored.read_bytes())
    data[-1] ^= 0xFF  # same length, so only the CRC32C / content check can catch it
    stored.write_bytes(bytes(data))
    assert _verify_v2(db_session, media_file, record, parts, backend) == "mismatch"


def test_local_file_modified_since_backup_is_changed_not_mismatch(db_session, catalog, tmp_path):
    media_file, record, parts, backend, path = _backed_up_file(db_session, catalog, tmp_path, encrypted=False)
    path.write_bytes(b"edited after the backup ran")
    assert _verify_v2(db_session, media_file, record, parts, backend) == "changed"


def test_local_file_gone_still_verifies_the_bucket_copy(db_session, catalog, tmp_path):
    media_file, record, parts, backend, path = _backed_up_file(db_session, catalog, tmp_path, encrypted=False)
    path.unlink()
    assert _verify_v2(db_session, media_file, record, parts, backend) == "match"


def test_record_without_sha256_is_mismatch(db_session, catalog, tmp_path):
    media_file, record, parts, backend, _ = _backed_up_file(db_session, catalog, tmp_path, encrypted=False)
    record.sha256 = None
    assert _verify_v2(db_session, media_file, record, parts, backend) == "mismatch"


def test_missing_index_file_is_missing(db_session, catalog, tmp_path):
    media_file, record, parts, backend, _ = _backed_up_file(db_session, catalog, tmp_path, encrypted=False)
    archive = parts[0][1]
    backend.delete(index_object_key(archive.archive_id, archive.path[: archive.path.rfind("archives/")]))
    assert _verify_v2(db_session, media_file, record, parts, backend) == "missing"


def test_index_that_disagrees_with_database_is_mismatch(db_session, catalog, tmp_path):
    media_file, record, parts, backend, _ = _backed_up_file(db_session, catalog, tmp_path, encrypted=False)
    archive = parts[0][1]
    idx_path = backend._path(index_object_key(archive.archive_id, archive.path[: archive.path.rfind("archives/")]))
    index = json.loads(idx_path.read_text())
    index["files"] = {}  # the file's hash is no longer listed
    idx_path.write_text(json.dumps(index))
    assert _verify_v2(db_session, media_file, record, parts, backend) == "mismatch"
