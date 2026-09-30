"""Tests for the v2 restore path added to worker/app/tasks.py
(docs/cfa-spec.md section 7). See docs/backup-plan/steps/07-restore.md.

Database-backed: every test here needs MEDIABRIDGE_TEST_DB=1 (see
tests/conftest.py's db_session fixture). Several tests produce their fixture
data by actually running step 06's _execute_backup_run against a
LocalBackend, then restoring from that same backend - a real round trip
rather than hand-built rows. No test contacts a real bucket.
"""
from __future__ import annotations

import os

import pytest

from app.backup_run import _execute_backup_run
from app.models import BackupArchive, BackupRecord, BackupRecordArchive
from app.tasks import _restore_v2, _v2_ledger_parts
from tests.conftest import (
    set_cloud_config,
    set_encryption_enabled,
    set_transfer_config,
    usable_destination,
    write_file,
)
from tests.storage_double import LocalBackend

pytestmark = pytest.mark.usefixtures(
    "encryption_config_state", "cloud_storage_config_state", "transfer_config_state"
)


def _backup(db_session, source, destination, backend, **transfer_overrides):
    set_encryption_enabled(db_session, False)
    set_cloud_config(db_session)
    if transfer_overrides:
        set_transfer_config(db_session, **transfer_overrides)
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"
    return result


# --- round trips (through step 06's real backup output) ---------------------


def test_restores_file_backed_up_as_clump(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    set_transfer_config(db_session, min_size_bytes=1000, clump_size_bytes=100_000, max_size_bytes=2_000_000)

    content_a = b"a" * 200
    content_b = b"b" * 300
    write_file(tmp_path, "a.txt", content_a)
    write_file(tmp_path, "b.txt", content_b)
    mf_a = catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=len(content_a)
    )
    mf_b = catalog.make_media_file(
        path=str(tmp_path / "b.txt"), filename="b.txt", storage_location_id=source.id, size_bytes=len(content_b)
    )

    backend = LocalBackend(tmp_path / "bucket")
    _backup(db_session, source, destination, backend)

    restore_root = tmp_path / "restored"
    for media_file, content in [(mf_a, content_a), (mf_b, content_b)]:
        output_path = restore_root / media_file.filename
        v2 = _v2_ledger_parts(db_session, media_file.id, destination.id)
        assert v2 is not None
        record, parts = v2
        _restore_v2(db_session, media_file, record, parts, output_path, backend)

        assert output_path.read_bytes() == content

    # restoring one file from a shared clump must not disturb the other.
    assert (restore_root / "a.txt").read_bytes() == content_a
    assert (restore_root / "b.txt").read_bytes() == content_b


def test_restores_single_archive(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    set_transfer_config(db_session, min_size_bytes=100, clump_size_bytes=100_000, max_size_bytes=2_000_000)

    content = os.urandom(50_000)
    write_file(tmp_path, "movie.mp4", content)
    media_file = catalog.make_media_file(
        path=str(tmp_path / "movie.mp4"), filename="movie.mp4", storage_location_id=source.id, size_bytes=len(content)
    )

    backend = LocalBackend(tmp_path / "bucket")
    _backup(db_session, source, destination, backend)

    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).all()
    assert [a.archive_type for a in archives] == ["single"]

    output_path = tmp_path / "restored" / "movie.mp4"
    v2 = _v2_ledger_parts(db_session, media_file.id, destination.id)
    assert v2 is not None
    record, parts = v2
    _restore_v2(db_session, media_file, record, parts, output_path, backend)

    assert output_path.read_bytes() == content


def test_restores_split_file_reassembling_parts_in_order(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    max_size = 2 * 1024 * 1024
    set_transfer_config(db_session, min_size_bytes=1000, clump_size_bytes=max_size, max_size_bytes=max_size)

    size = int(1.5 * 1024 * 1024) + 1  # forces an uneven 2-way split, same as step 06's own test
    content = os.urandom(size)
    write_file(tmp_path, "big.bin", content)
    media_file = catalog.make_media_file(
        path=str(tmp_path / "big.bin"), filename="big.bin", storage_location_id=source.id, size_bytes=size
    )

    backend = LocalBackend(tmp_path / "bucket")
    _backup(db_session, source, destination, backend)

    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id, archive_type="part").all()
    assert len(archives) == 2

    output_path = tmp_path / "restored" / "big.bin"
    v2 = _v2_ledger_parts(db_session, media_file.id, destination.id)
    assert v2 is not None
    record, parts = v2
    assert len(parts) == 2
    _restore_v2(db_session, media_file, record, parts, output_path, backend)

    assert output_path.read_bytes() == content


# --- verification -------------------------------------------------------


def test_restore_rejects_content_that_fails_sha256_verification(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    set_transfer_config(db_session, min_size_bytes=100, clump_size_bytes=100_000, max_size_bytes=2_000_000)

    content = b"pristine content"
    write_file(tmp_path, "a.txt", content)
    media_file = catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=len(content)
    )

    backend = LocalBackend(tmp_path / "bucket")
    _backup(db_session, source, destination, backend)

    v2 = _v2_ledger_parts(db_session, media_file.id, destination.id)
    assert v2 is not None
    record, parts = v2
    link, archive = parts[0]

    # Simulate undetected bucket-side corruption: flip the first payload byte
    # directly on the backend's stored object, bypassing upload_and_confirm
    # entirely. Flipping *some* byte outside archive_offset..+archive_length
    # (e.g. the tar's own trailing padding) wouldn't be caught by a ranged
    # read, so this must land inside the payload range restore actually reads.
    stored_path = backend.root / archive.path
    data = bytearray(stored_path.read_bytes())
    data[link.archive_offset] ^= 0xFF
    stored_path.write_bytes(bytes(data))

    output_path = tmp_path / "restored" / "a.txt"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"old content that must survive a failed restore")

    with pytest.raises(RuntimeError, match="sha256"):
        _restore_v2(db_session, media_file, record, parts, output_path, backend)

    assert output_path.read_bytes() == b"old content that must survive a failed restore"
    assert not output_path.with_name(output_path.name + ".mbcopy").exists()


# --- lookup ---------------------------------------------------------------


def test_v2_ledger_parts_ignores_incomplete_archive(db_session, catalog):
    source = catalog.make_storage_location()
    destination = usable_destination(catalog)
    media_file = catalog.make_media_file(storage_location_id=source.id)

    archive = BackupArchive(
        storage_location_id=destination.id,
        path="prefix/archives/incomplete.tar",
        size_bytes=10,
        archive_id="11111111-1111-1111-1111-111111111111",
        archive_type="single",
        crc32c="AAAAAA==",
        indexed_at=None,  # the incomplete case
    )
    db_session.add(archive)
    db_session.flush()
    record = BackupRecord(
        media_file_id=media_file.id,
        destination_storage_location_id=destination.id,
        local_path=media_file.path,
        local_checksum="deadbeef",
        sha256="a" * 64,
    )
    db_session.add(record)
    db_session.flush()
    db_session.add(
        BackupRecordArchive(
            backup_record_id=record.id, backup_archive_id=archive.id, part_index=0, archive_offset=0, archive_length=10
        )
    )
    db_session.commit()

    assert _v2_ledger_parts(db_session, media_file.id, destination.id) is None


def test_v2_ledger_parts_returns_none_for_legacy_only_backup(db_session, catalog):
    source = catalog.make_storage_location()
    destination = usable_destination(catalog)
    media_file = catalog.make_media_file(storage_location_id=source.id)

    archive = BackupArchive(
        storage_location_id=destination.id,
        path="gcs://bucket/some/legacy/path",
        size_bytes=10,
        archive_id=None,  # the legacy shape - no v2 columns set
        archive_type=None,
        crc32c=None,
        indexed_at=None,
    )
    db_session.add(archive)
    db_session.flush()
    record = BackupRecord(
        media_file_id=media_file.id,
        destination_storage_location_id=destination.id,
        local_path=media_file.path,
        local_checksum="deadbeef",
    )
    db_session.add(record)
    db_session.flush()
    db_session.add(
        BackupRecordArchive(
            backup_record_id=record.id, backup_archive_id=archive.id, part_index=0, archive_offset=0, archive_length=10
        )
    )
    db_session.commit()

    assert _v2_ledger_parts(db_session, media_file.id, destination.id) is None


# --- wiring -----------------------------------------------------------------
