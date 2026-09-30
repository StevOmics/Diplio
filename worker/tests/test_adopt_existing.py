"""Cross-run dedupe: content already stored at the archive is recorded, not
re-uploaded. DB-backed (scratch DB). See backup_run._adopt_existing_copies."""
from __future__ import annotations

import os

import pytest

from app.backup_run import _execute_backup_run
from app.models import BackupArchive, BackupRecord, BackupRecordArchive, MediaFile
from app.tasks import _restore_v2, _v2_ledger_parts
from tests.conftest import set_cloud_config, set_encryption_enabled, set_encryption_key, set_transfer_config, usable_destination
from tests.storage_double import CountingBackend, LocalBackend

pytestmark = pytest.mark.usefixtures("encryption_config_state", "cloud_storage_config_state", "transfer_config_state")


class _Lib:
    def __init__(self, db_session, catalog, tmp_path, *, encrypted, **transfer):
        self.db, self.catalog, self.tmp = db_session, catalog, tmp_path
        self.source = catalog.make_storage_location(path=str(tmp_path / "src"))
        self.destination = usable_destination(catalog)
        set_cloud_config(db_session)
        settings = dict(min_size_bytes=100, clump_size_bytes=100_000_000, max_size_bytes=8_000_000)
        settings.update(transfer)
        set_transfer_config(db_session, **settings)
        if encrypted:
            set_encryption_key(db_session)
        set_encryption_enabled(db_session, encrypted)
        self.backend = CountingBackend(LocalBackend(tmp_path / "bucket"))

    def add(self, rel: str, data: bytes) -> MediaFile:
        path = self.tmp / "src" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.catalog.make_media_file(
            path=str(path), filename=path.name, storage_location_id=self.source.id, size_bytes=len(data)
        )

    def run(self) -> dict:
        self.backend.reset()
        return _execute_backup_run(self.source.id, self.destination.id, backend=self.backend, upload_sleep=lambda s: None)

    def record(self, media_file) -> BackupRecord:
        self.db.expire_all()
        return self.db.query(BackupRecord).filter_by(media_file_id=media_file.id, destination_storage_location_id=self.destination.id).one()

    def restore(self, media_file) -> bytes:
        record, parts = _v2_ledger_parts(self.db, media_file.id, self.destination.id)
        out = self.tmp / "restored" / f"{media_file.id}.out"
        _restore_v2(self.db, media_file, record, parts, out, self.backend)
        return out.read_bytes()

    def delete_archive_object(self, media_file) -> None:
        """Simulates an out-of-band deletion straight from the bucket (not
        through this app): removes the underlying file for every archive
        object linked to media_file's record, leaving the DB rows as-is."""
        record = self.record(media_file)
        for (path,) in (
            self.db.query(BackupArchive.path)
            .join(BackupRecordArchive, BackupRecordArchive.backup_archive_id == BackupArchive.id)
            .filter(BackupRecordArchive.backup_record_id == record.id)
        ):
            (self.tmp / "bucket" / path).unlink(missing_ok=True)


@pytest.mark.parametrize("encrypted", [False, True])
def test_a_renamed_file_is_recorded_not_uploaded(db_session, catalog, tmp_path, encrypted):
    lib = _Lib(db_session, catalog, tmp_path, encrypted=encrypted)
    data = os.urandom(5000)
    original = lib.add("a/photo.bin", data)
    assert lib.run()["files"] == 1

    copy = lib.add("b/renamed.bin", data)
    result = lib.run()
    assert (result["files"], result["adopted"], result["status"]) == (0, 1, "ok")
    assert lib.backend.total_ops == 1 and lib.backend.ops.get("list") == 1  # one batched listing, no uploads
    assert sorted(s["reason"] for s in result["skipped"]) == ["already_backed_up", "unchanged"]  # the copy, and the original

    r_orig, r_copy = lib.record(original), lib.record(copy)
    assert r_copy.sha256 == r_orig.sha256 and r_copy.status == "done" and r_copy.mtime_ns is not None
    assert lib.restore(copy) == data and lib.restore(original) == data
    assert lib.db.query(BackupArchive).filter_by(storage_location_id=lib.destination.id).count() == 1  # nothing new stored

    again = lib.run()  # and both are now plain "unchanged"
    assert (again["files"], again["adopted"]) == (0, 0) and lib.backend.total_ops == 1 and lib.backend.ops.get("list") == 1


def test_a_touched_file_with_the_same_content_only_updates_its_record(db_session, catalog, tmp_path):
    lib = _Lib(db_session, catalog, tmp_path, encrypted=True)
    f = lib.add("a.bin", os.urandom(4000))
    lib.run()
    links_before = [(l.backup_archive_id, l.archive_offset) for l in db_session.query(BackupRecordArchive).filter_by(backup_record_id=lib.record(f).id)]

    path = tmp_path / "src" / "a.bin"
    os.utime(path, ns=(1, path.stat().st_mtime_ns + 9_000_000_000))  # mtime moves, content doesn't
    result = lib.run()
    assert (result["files"], result["adopted"]) == (0, 1) and lib.backend.total_ops == 1 and lib.backend.ops.get("list") == 1

    rec = lib.record(f)
    assert rec.mtime_ns == path.stat().st_mtime_ns
    assert [(l.backup_archive_id, l.archive_offset) for l in db_session.query(BackupRecordArchive).filter_by(backup_record_id=rec.id)] == links_before
    assert lib.run()["adopted"] == 0  # settled


def test_a_split_file_copy_shares_all_of_its_parts(db_session, catalog, tmp_path):
    lib = _Lib(db_session, catalog, tmp_path, encrypted=False, max_size_bytes=4_000_000)
    data = os.urandom(9_000_000)
    lib.add("big.bin", data)
    lib.run()
    copy = lib.add("copy/big.bin", data)
    assert lib.run()["adopted"] == 1 and lib.backend.total_ops == 1 and lib.backend.ops.get("list") == 1
    assert db_session.query(BackupRecordArchive).filter_by(backup_record_id=lib.record(copy).id).count() >= 3
    assert lib.restore(copy) == data


def test_a_changed_file_is_still_uploaded(db_session, catalog, tmp_path):
    lib = _Lib(db_session, catalog, tmp_path, encrypted=False)
    lib.add("a.bin", os.urandom(3000))
    lib.run()
    lib.add("b.bin", os.urandom(3000))  # different bytes: nothing to adopt
    result = lib.run()
    assert (result["files"], result["adopted"]) == (1, 0) and lib.backend.bytes_up > 0


def test_turning_encryption_on_does_not_adopt_a_plaintext_copy(db_session, catalog, tmp_path):
    lib = _Lib(db_session, catalog, tmp_path, encrypted=False)
    data = os.urandom(3000)
    lib.add("a.bin", data)
    lib.run()
    set_encryption_key(db_session)  # now the user wants everything encrypted
    copy = lib.add("b.bin", data)
    result = lib.run()
    assert (result["files"], result["adopted"]) == (1, 0)
    assert all(a.encrypted for a in db_session.query(BackupArchive).join(BackupRecordArchive, BackupRecordArchive.backup_archive_id == BackupArchive.id).filter(BackupRecordArchive.backup_record_id == lib.record(copy).id))


def test_a_rotated_key_does_not_adopt_data_encrypted_under_the_old_key(db_session, catalog, tmp_path):
    lib = _Lib(db_session, catalog, tmp_path, encrypted=True)
    data = os.urandom(3000)
    lib.add("a.bin", data)
    lib.run()
    set_encryption_key(db_session, password="a-different-passphrase")
    copy = lib.add("b.bin", data)
    result = lib.run()
    assert (result["files"], result["adopted"]) == (1, 0)
    assert lib.restore(copy) == data


def test_an_incomplete_archive_is_never_reused(db_session, catalog, tmp_path):
    lib = _Lib(db_session, catalog, tmp_path, encrypted=False)
    data = os.urandom(3000)
    lib.add("a.bin", data)
    lib.run()
    db_session.query(BackupArchive).filter_by(storage_location_id=lib.destination.id).update({"indexed_at": None})
    db_session.commit()
    lib.add("b.bin", data)
    assert lib.run()["adopted"] == 0


def test_duplicates_within_one_run_are_still_stored_once(db_session, catalog, tmp_path):
    lib = _Lib(db_session, catalog, tmp_path, encrypted=False)
    data = os.urandom(4000)
    lib.add("x/a.bin", data)
    lib.add("y/b.bin", data)
    result = lib.run()
    assert result["files"] == 2 and result["adopted"] == 0  # the packer dedupes these
    # one stored member serves both paths
    assert db_session.query(BackupArchive).filter_by(storage_location_id=lib.destination.id).count() == 1
    offsets = {l.archive_offset for l in db_session.query(BackupRecordArchive)}
    assert len(offsets) == 1


def test_a_file_whose_archive_object_was_deleted_out_of_band_is_reuploaded(db_session, catalog, tmp_path):
    """Regression for the bug where deleting a library's archived content
    directly from the bucket, then re-running a backup, uploaded nothing:
    _is_unchanged trusted local mtime/size alone and never noticed the
    archive object itself was gone."""
    lib = _Lib(db_session, catalog, tmp_path, encrypted=False)
    f = lib.add("a.bin", os.urandom(3000))
    assert lib.run()["files"] == 1

    lib.delete_archive_object(f)  # simulate someone deleting it straight from the bucket
    result = lib.run()
    assert result["files"] == 1  # re-backed-up, not skipped as "unchanged"
    assert not any(s["reason"] == "unchanged" for s in result["skipped"])


def test_adopt_existing_copies_does_not_adopt_a_source_whose_archive_object_is_gone(db_session, catalog, tmp_path):
    lib = _Lib(db_session, catalog, tmp_path, encrypted=False)
    data = os.urandom(3000)
    original = lib.add("a.bin", data)
    assert lib.run()["files"] == 1

    lib.delete_archive_object(original)
    copy = lib.add("b.bin", data)
    result = lib.run()
    # Neither the original's own "unchanged" shortcut nor the copy's adopt
    # path may trust the deleted archive object: both need a real re-upload.
    assert result["adopted"] == 0
    assert result["files"] == 2
    assert lib.restore(copy) == data and lib.restore(original) == data


def test_regression_normal_unchanged_and_adopt_paths_do_one_listing_per_run(db_session, catalog, tmp_path):
    """The fix must not turn per-file bucket calls back on: exactly one
    list_stats call per run, no matter how many files are unchanged/adopted."""
    lib = _Lib(db_session, catalog, tmp_path, encrypted=False)
    data = os.urandom(3000)
    original = lib.add("a.bin", data)
    lib.add("b.bin", os.urandom(3000))
    assert lib.run()["files"] == 2

    copy = lib.add("c.bin", data)  # will be adopted
    lib.backend.reset()
    result = lib.run()
    assert result["adopted"] == 1  # the copy
    assert sorted(s["reason"] for s in result["skipped"]) == ["already_backed_up", "unchanged", "unchanged"]
    assert lib.backend.ops.get("list") == 1 and lib.backend.total_ops == 1
    assert lib.restore(copy) == data
