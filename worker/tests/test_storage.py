"""Tests for worker/app/storage.py (docs/cfa-spec.md sections 4, 6.4, 6 retry
rule) - the StorageBackend protocol, CRC32C helpers, and upload_and_confirm.

Pure logic + tmp_path only. No bucket, no database - every test runs against
LocalBackend/FlakyBackend/CorruptingBackend from tests/storage_double.py. See
docs/backup-plan/steps/05-storage-and-upload.md.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.storage import (
    ObjectStat,
    UploadVerificationError,
    crc32c_base64,
    crc32c_base64_file,
    upload_and_confirm,
)
from tests.storage_double import CorruptingBackend, FlakyBackend, LocalBackend


# --- CRC32C -----------------------------------------------------------------


def test_crc32c_base64_known_vector():
    # Verified in this environment (see step 05's notes): the base64 of
    # google_crc32c's big-endian digest for this exact input.
    assert crc32c_base64(b"hello world") == "yZRlqg=="


def test_crc32c_base64_empty():
    # Derived once with google_crc32c.Checksum().digest() on b"" and
    # hard-coded here, per the step file's instruction.
    assert crc32c_base64(b"") == "AAAAAA=="


def test_crc32c_file_matches_bytes(tmp_path):
    data = os.urandom(10 * 1024 * 1024)
    path = tmp_path / "big.bin"
    path.write_bytes(data)

    assert crc32c_base64_file(path) == crc32c_base64(data)


# --- LocalBackend -------------------------------------------------------------


def test_upload_download_roundtrip(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    src = tmp_path / "src.bin"
    src.write_bytes(b"some archive bytes")

    backend.upload("archives/a.tar", src)

    dest = tmp_path / "dest.bin"
    backend.download("archives/a.tar", dest)
    assert dest.read_bytes() == b"some archive bytes"


def test_write_bytes_then_download(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    backend.write_bytes("index/a.json", b'{"archive_id": "x"}')

    dest = tmp_path / "dest.json"
    backend.download("index/a.json", dest)
    assert dest.read_bytes() == b'{"archive_id": "x"}'


def test_read_range_matches_slices(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    data = bytes(range(256)) * 4  # 1024 distinct-ish bytes
    backend.write_bytes("blob", data)

    assert backend.read_range("blob", 0, 10) == data[0:10]
    assert backend.read_range("blob", 100, 50) == data[100:150]
    assert backend.read_range("blob", 5, 0) == b""
    assert backend.read_range("blob", len(data) - 1, 1) == data[-1:]


def test_read_range_past_end_raises(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    backend.write_bytes("blob", b"0123456789")

    with pytest.raises(Exception):
        backend.read_range("blob", 5, 100)

    with pytest.raises(Exception):
        backend.read_range("blob", 11, 1)


def test_stat_reports_size_and_crc32c(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    data = b"hello world"
    backend.write_bytes("blob", data)

    stat = backend.stat("blob")
    assert stat == ObjectStat(size=len(data), crc32c="yZRlqg==")


def test_delete_is_idempotent(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    backend.delete("never-existed")  # must not raise

    backend.write_bytes("blob", b"x")
    backend.delete("blob")
    backend.delete("blob")  # second delete of an already-gone key: still fine
    assert not backend.exists("blob")


def test_exists(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    assert not backend.exists("blob")
    backend.write_bytes("blob", b"x")
    assert backend.exists("blob")


# --- upload_and_confirm -------------------------------------------------------


def _archive(tmp_path, data=b"archive payload bytes"):
    path = tmp_path / "archive.tar"
    path.write_bytes(data)
    return path


def _norecord_sleep(_seconds):
    pass


def test_confirms_and_returns_stat(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    archive = _archive(tmp_path)

    stat = upload_and_confirm(backend, "archives/a.tar", archive, sleep=_norecord_sleep)

    assert stat == ObjectStat(size=archive.stat().st_size, crc32c=crc32c_base64_file(archive))
    assert backend.exists("archives/a.tar")


def test_retries_then_succeeds(tmp_path):
    inner = LocalBackend(tmp_path / "bucket")
    flaky = FlakyBackend(inner, fail_uploads_on={1, 2}, exc=ConnectionError("boom"))
    archive = _archive(tmp_path)

    stat = upload_and_confirm(flaky, "archives/a.tar", archive, sleep=_norecord_sleep)

    assert flaky.upload_calls == 3
    assert stat == ObjectStat(size=archive.stat().st_size, crc32c=crc32c_base64_file(archive))


def test_exhausts_attempts_and_raises(tmp_path):
    inner = LocalBackend(tmp_path / "bucket")
    flaky = FlakyBackend(inner, fail_uploads_on={1, 2, 3, 4, 5}, exc=ConnectionError("boom"))
    archive = _archive(tmp_path)

    with pytest.raises(UploadVerificationError):
        upload_and_confirm(flaky, "archives/a.tar", archive, sleep=_norecord_sleep)

    assert flaky.upload_calls == 5


def test_crc_mismatch_is_not_treated_as_success(tmp_path):
    inner = LocalBackend(tmp_path / "bucket")
    corrupting = CorruptingBackend(inner)
    archive = _archive(tmp_path)

    with pytest.raises(UploadVerificationError):
        upload_and_confirm(corrupting, "archives/a.tar", archive, sleep=_norecord_sleep)


def test_error_message_names_key_and_both_checksums(tmp_path):
    inner = LocalBackend(tmp_path / "bucket")
    corrupting = CorruptingBackend(inner)
    archive = _archive(tmp_path)
    expected_crc32c = crc32c_base64_file(archive)

    with pytest.raises(UploadVerificationError) as excinfo:
        upload_and_confirm(corrupting, "archives/a.tar", archive, sleep=_norecord_sleep)

    message = str(excinfo.value)
    assert "archives/a.tar" in message
    assert expected_crc32c in message


def test_backoff_sleeps_between_attempts(tmp_path):
    inner = LocalBackend(tmp_path / "bucket")
    flaky = FlakyBackend(inner, fail_uploads_on={1, 2, 3, 4, 5}, exc=ConnectionError("boom"))
    archive = _archive(tmp_path)

    delays = []

    def recording_sleep(seconds):
        delays.append(seconds)

    with pytest.raises(UploadVerificationError):
        upload_and_confirm(flaky, "archives/a.tar", archive, sleep=recording_sleep)

    assert len(delays) == 4  # attempts - 1
    # Base delays (2 ** i) are strictly increasing even with jitter added,
    # since consecutive integer bases are at least 1 apart and jitter is < 1.
    assert delays == sorted(delays)
    assert all(b < a for a, b in zip(delays[1:], delays[:-1]))


def test_local_checksum_computed_once(tmp_path, monkeypatch):
    inner = LocalBackend(tmp_path / "bucket")
    flaky = FlakyBackend(inner, fail_uploads_on={1, 2}, exc=ConnectionError("boom"))
    archive = _archive(tmp_path)

    calls = []
    import app.storage as storage_module

    real_crc32c_base64_file = storage_module.crc32c_base64_file

    def counting(path):
        calls.append(path)
        return real_crc32c_base64_file(path)

    monkeypatch.setattr(storage_module, "crc32c_base64_file", counting)

    upload_and_confirm(flaky, "archives/a.tar", archive, sleep=_norecord_sleep)

    assert len(calls) == 1
