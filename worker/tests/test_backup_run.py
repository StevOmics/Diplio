"""Tests for worker/app/backup_run.py - the v2 backup run orchestrator
(docs/cfa-spec.md section 6). See docs/backup-plan/steps/06-backup-run.md.

Database-backed: every test here needs MEDIABRIDGE_TEST_DB=1 (see
tests/conftest.py's db_session fixture) and runs against the same Postgres
the dev stack uses, via the `catalog`/`*_config_state` fixtures that scope
and clean up exactly what each test creates. No test contacts a real bucket -
everything storage-related uses LocalBackend/FlakyBackend from
tests/storage_double.py.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.backup_run import BACKUP_RUN_LOCK_KEY, BackupRunRefused, _execute_backup_run
from app.db import engine
from app.models import BackupArchive, BackupEncryptionConfig, BackupRecord, BackupRecordArchive, CloudStorageConfig
from app.storage import UploadVerificationError
from tests.conftest import (
    set_cloud_config as _set_cloud_config,
    set_encryption_enabled as _set_encryption_enabled,
    set_transfer_config as _set_transfer_config,
    usable_destination as _usable_destination,
    write_file as _write_file,
)
from tests.storage_double import FlakyBackend, LocalBackend

pytestmark = pytest.mark.usefixtures(
    "encryption_config_state", "cloud_storage_config_state", "transfer_config_state"
)


def _acquire_lock(conn_key=BACKUP_RUN_LOCK_KEY):
    conn = engine.connect()
    conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": conn_key})
    return conn


def _release_lock(conn, conn_key=BACKUP_RUN_LOCK_KEY):
    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": conn_key})
    conn.close()


# --- refusals -------------------------------------------------------------


def test_refuses_when_encryption_enabled_without_password(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_cloud_config(db_session)
    _set_encryption_enabled(db_session, True)
    config = db_session.query(BackupEncryptionConfig).first()
    config.password = None
    db_session.commit()

    backend = LocalBackend(tmp_path / "bucket")
    with pytest.raises(BackupRunRefused):
        _execute_backup_run(source.id, destination.id, backend=backend)

    assert not (tmp_path / "bucket").exists() or list((tmp_path / "bucket").rglob("*")) == []


def test_refuses_without_cloud_config(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    db_session.query(CloudStorageConfig).delete(synchronize_session=False)
    db_session.commit()

    backend = LocalBackend(tmp_path / "bucket")
    with pytest.raises(BackupRunRefused):
        _execute_backup_run(source.id, destination.id, backend=backend)


def test_refuses_when_destination_is_not_a_backup_target(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = catalog.make_storage_location(location_type="gcs", is_backup_target=False)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)

    backend = LocalBackend(tmp_path / "bucket")
    with pytest.raises(BackupRunRefused):
        _execute_backup_run(source.id, destination.id, backend=backend)


# --- locking ----------------------------------------------------------------


def test_second_run_skips_while_lock_held(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    lock_conn = _acquire_lock()
    try:
        backend = LocalBackend(tmp_path / "bucket")
        result = _execute_backup_run(source.id, destination.id, backend=backend)
        assert result == {"status": "skipped", "reason": "another run holds the lock"}
        assert list((tmp_path / "bucket").rglob("*")) == []
    finally:
        _release_lock(lock_conn)


def test_lock_released_on_success(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"

    check_conn = engine.connect()
    try:
        got = check_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": BACKUP_RUN_LOCK_KEY}).scalar()
        assert got is True
    finally:
        check_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BACKUP_RUN_LOCK_KEY})
        check_conn.close()


def test_lock_released_on_failure(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    backend = FlakyBackend(
        LocalBackend(tmp_path / "bucket"), fail_uploads_on={1, 2, 3, 4, 5}, exc=RuntimeError("boom")
    )
    with pytest.raises(UploadVerificationError):
        _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    check_conn = engine.connect()
    try:
        got = check_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": BACKUP_RUN_LOCK_KEY}).scalar()
        assert got is True
    finally:
        check_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BACKUP_RUN_LOCK_KEY})
        check_conn.close()


def test_lock_uses_a_dedicated_connection(monkeypatch, db_session, catalog, tmp_path):
    """The ORM session's connection must never be the one that runs the
    advisory lock SQL - see docs/backup-plan/HANDOFF.md's first trap. Proven
    here by spying on every ORM Session.execute() call (which is what
    SessionLocal()-created sessions use) and asserting none of them ever see
    advisory-lock SQL; only a bare engine.connect() Core connection should."""
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    original_execute = Session.execute
    advisory_calls_on_orm_sessions = []

    def spying_execute(self, statement, *args, **kwargs):
        if "advisory" in str(statement).lower():
            advisory_calls_on_orm_sessions.append(str(statement))
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", spying_execute)

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    assert result["status"] == "ok"
    assert advisory_calls_on_orm_sessions == []


# --- ordering ----------------------------------------------------------------


class _RecordingBackend:
    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, str]] = []

    def upload(self, key, local_path, *, max_bytes_per_sec=None, progress_cb=None):
        self.calls.append(("upload", key))
        self._inner.upload(key, local_path, max_bytes_per_sec=max_bytes_per_sec, progress_cb=progress_cb)

    def write_bytes(self, key, data, *, content_type="application/json"):
        self.calls.append(("write_bytes", key))
        self._inner.write_bytes(key, data, content_type=content_type)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_index_written_only_after_upload_confirmed(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"x" * 100)
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=100
    )

    backend = _RecordingBackend(LocalBackend(tmp_path / "bucket"))
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"

    upload_pos = {key: i for i, (op, key) in enumerate(backend.calls) if op == "upload"}
    write_pos = {key: i for i, (op, key) in enumerate(backend.calls) if op == "write_bytes"}
    assert upload_pos and write_pos
    for index_key, pos in write_pos.items():
        archive_key = index_key.replace("index/", "archives/").replace(".json", ".tar")
        assert archive_key in upload_pos
        assert upload_pos[archive_key] < pos


def test_failed_upload_writes_no_index_and_no_rows(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"x" * 100)
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=100
    )

    backend = FlakyBackend(
        LocalBackend(tmp_path / "bucket"), fail_uploads_on={1, 2, 3, 4, 5}, exc=RuntimeError("boom")
    )
    with pytest.raises(UploadVerificationError):
        _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    bucket_index_dir = tmp_path / "bucket" / "index"
    assert not bucket_index_dir.exists() or list(bucket_index_dir.glob("*.json")) == []
    assert (
        db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).count() == 0
    )


def test_rows_recorded_only_after_index(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"

    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).all()
    assert archives
    assert all(archive.indexed_at is not None for archive in archives)


# --- content -----------------------------------------------------------------


def test_small_files_clumped_and_recorded(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    # max_size_bytes must clear packer.payload_ceiling's 1 MiB margin.
    _set_transfer_config(db_session, min_size_bytes=1000, clump_size_bytes=100_000, max_size_bytes=2_000_000)

    content_a = b"a" * 200
    content_b = b"b" * 300
    _write_file(tmp_path, "a.txt", content_a)
    _write_file(tmp_path, "b.txt", content_b)
    mf_a = catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=len(content_a)
    )
    mf_b = catalog.make_media_file(
        path=str(tmp_path / "b.txt"), filename="b.txt", storage_location_id=source.id, size_bytes=len(content_b)
    )

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"

    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).all()
    assert len(archives) == 1
    assert archives[0].archive_type == "clump"

    for media_file, content in [(mf_a, content_a), (mf_b, content_b)]:
        record = (
            db_session.query(BackupRecord)
            .filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id)
            .one()
        )
        link = db_session.query(BackupRecordArchive).filter_by(backup_record_id=record.id).one()
        assert link.archive_length == len(content)
        assert backend.read_range(archives[0].path, link.archive_offset, link.archive_length) == content


def test_part_archive_length_is_part_size_not_whole_file(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    max_size = 2 * 1024 * 1024
    _set_transfer_config(db_session, min_size_bytes=1000, clump_size_bytes=max_size, max_size_bytes=max_size)

    size = int(1.5 * 1024 * 1024) + 1  # forces an uneven 2-way split
    content = os.urandom(size)
    _write_file(tmp_path, "big.bin", content)
    media_file = catalog.make_media_file(
        path=str(tmp_path / "big.bin"), filename="big.bin", storage_location_id=source.id, size_bytes=size
    )

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"

    archives = {
        archive.id: archive
        for archive in db_session.query(BackupArchive)
        .filter_by(storage_location_id=destination.id, archive_type="part")
        .all()
    }
    assert len(archives) == 2

    record = (
        db_session.query(BackupRecord)
        .filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id)
        .one()
    )
    links = (
        db_session.query(BackupRecordArchive)
        .filter_by(backup_record_id=record.id)
        .order_by(BackupRecordArchive.part_index)
        .all()
    )
    assert len(links) == 2
    # the whole gap this test exists to catch: neither part's recorded length
    # is the whole file's size.
    assert links[0].archive_length != size
    assert links[1].archive_length != size
    assert links[0].archive_length + links[1].archive_length == size

    reconstructed = b"".join(
        backend.read_range(archives[link.backup_archive_id].path, link.archive_offset, link.archive_length)
        for link in links
    )
    assert reconstructed == content


def test_excluded_files_are_skipped(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path), exclude_globs="*.tmp")
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "keep.txt", b"keep")
    _write_file(tmp_path, "skip.tmp", b"skip")
    catalog.make_media_file(
        path=str(tmp_path / "keep.txt"), filename="keep.txt", storage_location_id=source.id, size_bytes=4
    )
    catalog.make_media_file(
        path=str(tmp_path / "skip.tmp"), filename="skip.tmp", storage_location_id=source.id, size_bytes=4
    )

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    assert result["status"] == "ok"
    assert result["files"] == 1
    assert {"path": str(tmp_path / "skip.tmp"), "reason": "excluded"} in result["skipped"]


def test_changed_during_read_file_is_skipped_and_run_is_partial(monkeypatch, db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    path = _write_file(tmp_path, "flaky.txt", b"original")
    catalog.make_media_file(
        path=str(path), filename="flaky.txt", storage_location_id=source.id, size_bytes=8
    )

    import app.fingerprint as fingerprint_module

    real_sha256_file = fingerprint_module.sha256_file

    def _flaky_sha256_file(p):
        digest = real_sha256_file(p)
        Path(p).write_bytes(b"changed!!")
        return digest

    monkeypatch.setattr(fingerprint_module, "sha256_file", _flaky_sha256_file)

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    assert result["status"] == "partial"
    assert result["files"] == 0
    assert any(s["reason"] == "changed_during_read" for s in result["skipped"])


class _NonCleaningTempDir:
    """Stands in for tempfile.TemporaryDirectory but never removes the
    directory itself on exit, so the test can inspect what's left inside
    after the run returns."""

    def __init__(self, path: Path):
        self._path = path

    def __enter__(self):
        self._path.mkdir(parents=True, exist_ok=True)
        return str(self._path)

    def __exit__(self, *exc_info):
        return False


def test_temp_files_deleted(monkeypatch, db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    work_dir = tmp_path / "work"
    import app.backup_run as backup_run_module

    monkeypatch.setattr(
        backup_run_module.tempfile, "TemporaryDirectory", lambda prefix=None: _NonCleaningTempDir(work_dir)
    )

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    assert result["status"] == "ok"
    assert list(work_dir.glob("*.tar")) == []


def test_backup_record_keeps_legacy_checksum(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    media_file = catalog.make_media_file(
        path=str(tmp_path / "a.txt"),
        filename="a.txt",
        storage_location_id=source.id,
        size_bytes=5,
        fingerprint="legacy-fingerprint-value",
    )

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"

    record = (
        db_session.query(BackupRecord)
        .filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id)
        .one()
    )
    assert record.local_checksum == "legacy-fingerprint-value"
    assert record.sha256 is not None
    assert record.sha256 != record.local_checksum


# --- skip unchanged files (section 6.1) --------------------------------


def test_first_backup_of_a_file_is_never_skipped(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    assert result["status"] == "ok"
    assert result["files"] == 1
    assert result["skipped"] == []


def test_unchanged_file_is_skipped_and_not_reuploaded(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    media_file = catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    backend = LocalBackend(tmp_path / "bucket")
    first = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert first["status"] == "ok"
    assert first["files"] == 1

    second = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    assert second["status"] == "ok"
    assert second["files"] == 0
    assert second["archives"] == 0
    assert {"path": str(tmp_path / "a.txt"), "reason": "unchanged"} in second["skipped"]
    # exactly the one archive from the first run - nothing re-uploaded
    assert db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).count() == 1
    record = (
        db_session.query(BackupRecord)
        .filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id)
        .one()
    )
    assert record.sha256 is not None


def test_replace_all_mode_reuploads_an_unchanged_file(db_session, catalog, tmp_path):
    """BackupRun.mode == 'replace_all' bypasses _is_unchanged entirely: a file
    that would otherwise be skipped as unchanged is re-hashed and re-uploaded."""
    from app.models import BackupRun

    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    backend = LocalBackend(tmp_path / "bucket")
    first = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert first["status"] == "ok" and first["files"] == 1

    run = BackupRun(
        source_storage_location_id=source.id,
        destination_storage_location_id=destination.id,
        scope="library",
        mode="replace_all",
    )
    db_session.add(run)
    db_session.commit()

    second = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None, run_id=run.id)

    assert second["status"] == "ok"
    # force_all bypassed the unchanged shortcut (the file was hashed again),
    # but content-based dedup (_adopt_existing_copies) still recognized the
    # identical bytes and adopted the existing archive rather than
    # re-uploading - "replace all" disables the bookkeeping skip, not dedup.
    assert not any(s["reason"] == "unchanged" for s in second["skipped"])
    assert any(s["reason"] == "already_backed_up" for s in second["skipped"])
    assert db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).count() == 1


def test_replace_older_mode_still_skips_unchanged_files_by_default(db_session, catalog, tmp_path):
    """Regression check: mode='replace_older' (and the implicit default when
    unset) preserves today's skip-unchanged behavior."""
    from app.models import BackupRun

    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    _write_file(tmp_path, "a.txt", b"hello")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    backend = LocalBackend(tmp_path / "bucket")
    first = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert first["status"] == "ok" and first["files"] == 1

    run = BackupRun(
        source_storage_location_id=source.id,
        destination_storage_location_id=destination.id,
        scope="library",
        mode="replace_older",
    )
    db_session.add(run)
    db_session.commit()

    second = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None, run_id=run.id)

    assert second["status"] == "ok"
    assert second["files"] == 0
    assert {"path": str(tmp_path / "a.txt"), "reason": "unchanged"} in second["skipped"]
    assert db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).count() == 1


def test_changed_content_is_backed_up_again(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    path = _write_file(tmp_path, "a.txt", b"hello")
    media_file = catalog.make_media_file(
        path=str(path), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    backend = LocalBackend(tmp_path / "bucket")
    first = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert first["status"] == "ok"
    first_record_sha256 = (
        db_session.query(BackupRecord)
        .filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id)
        .one()
        .sha256
    )

    # A real edit: different content, later mtime, catalog re-scanned to match.
    new_content = b"hello, but different and longer now"
    path.write_bytes(new_content)
    later_ns = path.stat().st_mtime_ns + 10_000_000_000  # +10s, well clear of fs mtime granularity
    os.utime(path, ns=(later_ns, later_ns))
    media_file.size_bytes = len(new_content)
    db_session.commit()

    second = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    assert second["status"] == "ok"
    assert second["files"] == 1
    assert second["skipped"] == []
    db_session.refresh(media_file)
    record = (
        db_session.query(BackupRecord)
        .filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id)
        .one()
    )
    assert record.sha256 != first_record_sha256


def test_mismatched_size_alone_forces_rebackup(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _set_cloud_config(db_session)
    path = _write_file(tmp_path, "a.txt", b"hello")
    media_file = catalog.make_media_file(
        path=str(path), filename="a.txt", storage_location_id=source.id, size_bytes=5
    )

    backend = LocalBackend(tmp_path / "bucket")
    first = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert first["status"] == "ok"
    record = (
        db_session.query(BackupRecord)
        .filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id)
        .one()
    )
    recorded_mtime_ns = record.mtime_ns

    # Different, longer content - but mtime reset back to exactly what was
    # recorded, as if an edit happened to preserve it. media_file.size_bytes
    # is deliberately *not* updated here, standing in for a catalog that
    # hasn't been rescanned since - the check compares the real file's
    # current size against that (possibly stale) catalog value, not against
    # anything stored at backup time. Size alone must be enough to force a
    # re-backup: an `or` in place of `and` in the quick-check would wrongly
    # skip this, since mtime matches exactly.
    new_content = b"hello world, much longer content than before"
    path.write_bytes(new_content)
    os.utime(path, ns=(recorded_mtime_ns, recorded_mtime_ns))
    assert path.stat().st_mtime_ns == recorded_mtime_ns
    assert path.stat().st_size != media_file.size_bytes

    second = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    assert second["status"] == "ok"
    assert second["files"] == 1
    assert second["skipped"] == []
