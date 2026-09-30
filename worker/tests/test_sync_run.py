"""Sync runs (portability, step 18): a real backup written by one library,
then discovered and pulled down by a *second*, freshly-added library pointed
at the same archive - simulating a new instance/empty database finding an
existing bucket's content. DB-backed (MEDIABRIDGE_TEST_DB=1), scratch DB only.
"""
from __future__ import annotations

import os

import pytest

from app.backup_run import _execute_backup_run
from app.models import BackupArchive, BackupRecord, MediaFile, SyncRun
from app.sync_run import _execute_sync_run
from tests.conftest import (
    set_cloud_config,
    set_encryption_enabled,
    set_encryption_key,
    set_transfer_config,
    usable_destination,
    write_file,
)
from tests.storage_double import CountingBackend, LocalBackend

pytestmark = pytest.mark.usefixtures("encryption_config_state", "cloud_storage_config_state", "transfer_config_state")


def _seed_original_backup(db_session, catalog, tmp_path, files: dict[str, bytes], *, encrypted: bool):
    """The "old instance": backs `files` up for real from one library."""
    source = catalog.make_storage_location(path=str(tmp_path / "original"))
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_transfer_config(db_session, min_size_bytes=200, clump_size_bytes=100_000, max_size_bytes=2_000_000)
    if encrypted:
        set_encryption_key(db_session)
    set_encryption_enabled(db_session, encrypted)
    for name, data in files.items():
        path = tmp_path / "original" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        catalog.make_media_file(path=str(path), filename=path.name, storage_location_id=source.id, size_bytes=len(data))
    backend = CountingBackend(LocalBackend(tmp_path / "bucket"))
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"
    return destination, backend


def _new_library(catalog, destination, tmp_path):
    """The "new instance": an empty local folder, assigned to the *same*
    archive, with no catalog rows for its content - this is what discovery
    is for."""
    return catalog.make_storage_location(
        path=str(tmp_path / "newhome"), archive_location_id=destination.id, media_type="music"
    )


def _sync(db_session, library, destination, backend) -> SyncRun:
    run = SyncRun(library_storage_location_id=library.id, archive_storage_location_id=destination.id, status="queued")
    db_session.add(run)
    db_session.commit()
    result = _execute_sync_run(run.id, backend=backend)
    db_session.expire_all()
    return db_session.get(SyncRun, run.id), result


@pytest.mark.parametrize("encrypted", [False, True])
def test_syncs_files_from_a_bucket_this_instance_never_backed_up_to(db_session, catalog, tmp_path, encrypted):
    files = {"track1.mp3": os.urandom(50_000), "track2.mp3": os.urandom(80_000)}
    destination, backend = _seed_original_backup(db_session, catalog, tmp_path, files, encrypted=encrypted)
    new_library = _new_library(catalog, destination, tmp_path)

    run, result = _sync(db_session, new_library, destination, backend)

    assert result["status"] == "done" and result["synced"] == 2 and result["failed"] == 0
    assert run.status == "done" and run.files_synced == 2 and run.phase is None
    for name, data in files.items():
        target = tmp_path / "newhome" / name
        assert target.read_bytes() == data


def test_synced_files_are_fully_cataloged_and_recorded(db_session, catalog, tmp_path):
    files = {"track1.mp3": os.urandom(50_000)}
    destination, backend = _seed_original_backup(db_session, catalog, tmp_path, files, encrypted=True)
    new_library = _new_library(catalog, destination, tmp_path)

    _sync(db_session, new_library, destination, backend)

    target = str(tmp_path / "newhome" / "track1.mp3")
    media_file = db_session.query(MediaFile).filter_by(path=target).one()
    assert media_file.storage_location_id == new_library.id and media_file.media_type == "music"
    assert media_file.fingerprint  # computed from the actual restored bytes
    record = db_session.query(BackupRecord).filter_by(media_file_id=media_file.id, destination_storage_location_id=destination.id).one()
    assert record.status == "done" and record.sha256 and record.compression is None


def test_a_subsequent_backup_of_the_synced_library_uploads_nothing(db_session, catalog, tmp_path):
    """The point of writing real ledger rows, not just files on disk: once
    reconnected, a normal backup run recognizes the content is already
    stored (_adopt_existing_copies) and doesn't re-upload it."""
    files = {"track1.mp3": os.urandom(50_000), "track2.mp3": os.urandom(80_000)}
    destination, backend = _seed_original_backup(db_session, catalog, tmp_path, files, encrypted=True)
    new_library = _new_library(catalog, destination, tmp_path)
    _sync(db_session, new_library, destination, backend)

    for name in files:
        catalog.make_media_file(
            path=str(tmp_path / "newhome" / name), filename=name, storage_location_id=new_library.id,
            size_bytes=(tmp_path / "newhome" / name).stat().st_size,
        )
    backend.reset()
    result = _execute_backup_run(new_library.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    # One batched bucket listing per run (backup_run._run_locked) plus no
    # uploads/downloads for the adopted files.
    assert result["status"] == "ok" and result["adopted"] == 2 and backend.ops.get("list") == 1 and backend.total_ops == 1


def test_a_file_already_synced_is_skipped_not_resynced(db_session, catalog, tmp_path):
    files = {"track1.mp3": os.urandom(50_000)}
    destination, backend = _seed_original_backup(db_session, catalog, tmp_path, files, encrypted=False)
    new_library = _new_library(catalog, destination, tmp_path)
    first, _ = _sync(db_session, new_library, destination, backend)
    assert first.files_synced == 1

    second, result = _sync(db_session, new_library, destination, backend)
    assert result["status"] == "done" and result["synced"] == 0 and result["skipped"] == 1


def test_encrypted_content_with_no_matching_key_is_skipped_not_failed(db_session, catalog, tmp_path):
    files = {"track1.mp3": os.urandom(50_000)}
    destination, backend = _seed_original_backup(db_session, catalog, tmp_path, files, encrypted=True)
    new_library = _new_library(catalog, destination, tmp_path)
    set_encryption_key(db_session, password="a-completely-different-passphrase")  # the "new instance" doesn't know the real key

    run, result = _sync(db_session, new_library, destination, backend)

    assert result["status"] == "done" and result["synced"] == 0 and result["skipped"] == 1 and result["failed"] == 0
    assert not (tmp_path / "newhome" / "track1.mp3").exists()


def test_a_tampered_archive_is_reported_failed_and_writes_nothing(db_session, catalog, tmp_path):
    files = {"track1.mp3": os.urandom(50_000)}
    destination, backend = _seed_original_backup(db_session, catalog, tmp_path, files, encrypted=False)
    archive = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).one()
    stored = backend._inner._path(archive.path)
    raw = bytearray(stored.read_bytes())
    raw[-3] ^= 0xFF
    stored.write_bytes(bytes(raw))
    new_library = _new_library(catalog, destination, tmp_path)

    run, result = _sync(db_session, new_library, destination, backend)

    assert result["status"] == "failed" and result["failed"] == 1
    assert not (tmp_path / "newhome" / "track1.mp3").exists()
    assert not list((tmp_path / "newhome").glob("*.mbcopy"))
    assert db_session.query(MediaFile).filter_by(storage_location_id=new_library.id).count() == 0


def test_split_file_syncs_correctly(db_session, catalog, tmp_path):
    # _seed_original_backup sets max_size_bytes=2_000_000, so a 6 MB file is
    # already split into several "part" archives by the time it's backed up.
    data = os.urandom(6_000_000)
    destination, backend = _seed_original_backup(db_session, catalog, tmp_path, {"big.bin": data}, encrypted=False)
    assert db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).count() >= 3

    new_library = _new_library(catalog, destination, tmp_path)
    run, result = _sync(db_session, new_library, destination, backend)
    assert result["status"] == "done" and result["synced"] == 1
    assert (tmp_path / "newhome" / "big.bin").read_bytes() == data


def test_no_content_at_the_prefix_is_a_clean_done_run(db_session, catalog, tmp_path):
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    library = _new_library(catalog, destination, tmp_path)
    backend = LocalBackend(tmp_path / "bucket")
    run, result = _sync(db_session, library, destination, backend)
    assert result["status"] == "done" and result["synced"] == 0 and result["failed"] == 0


def test_progress_and_heartbeat_are_reported(db_session, catalog, tmp_path, monkeypatch):
    files = {"track1.mp3": os.urandom(50_000), "track2.mp3": os.urandom(80_000)}
    destination, backend = _seed_original_backup(db_session, catalog, tmp_path, files, encrypted=False)
    new_library = _new_library(catalog, destination, tmp_path)

    from app import sync_run as sync_run_module
    seen, real = [], sync_run_module._update_sync_run

    def spy(db, run_id, **fields):
        seen.append(dict(fields))
        return real(db, run_id, **fields)

    monkeypatch.setattr(sync_run_module, "_update_sync_run", spy)
    run, result = _sync(db_session, new_library, destination, backend)

    phases = [f for f in seen if f.get("phase")]
    assert phases and phases[0]["phase"] == "syncing"
    assert any(f.get("heartbeat_at") for f in seen)
    assert run.phase is None and run.status == "done"


def test_an_unknown_run_is_a_no_op():
    assert _execute_sync_run(999_999_999)["status"] == "missing"
