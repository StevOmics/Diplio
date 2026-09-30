"""Behaviour of the backup run and shallow verify against a library that is
already largely backed up: how many files get re-uploaded, and how many cloud
operations a sync / verify costs. DB-backed (MEDIABRIDGE_TEST_DB=1, scratch DB).

MB_SCALE_FILES sets the library size (default 300 so the normal suite stays
fast); set it to e.g. 20000 to see real-world numbers with `pytest -s`."""
from __future__ import annotations

import os
import random

import pytest
from sqlalchemy import event

from app.backup_run import _execute_backup_run
from app.db import engine
from app.models import BackupArchive, MediaFile
from app.tasks import _restore_v2, _v2_ledger_parts, _verify_v2_batch
from tests.conftest import set_cloud_config, set_encryption_enabled, set_encryption_key, set_transfer_config, usable_destination
from tests.storage_double import CountingBackend, LocalBackend

pytestmark = pytest.mark.usefixtures("encryption_config_state", "cloud_storage_config_state", "transfer_config_state")

N = int(os.environ.get("MB_SCALE_FILES", "300"))


class _Queries:
    def __init__(self):
        self.count = 0

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._hit)

    def _hit(self, *args, **kwargs):
        self.count += 1


def _build_library(db_session, catalog, tmp_path, n, *, encrypted, compress=False):
    source = catalog.make_storage_location(path=str(tmp_path / "src"))
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_transfer_config(
        db_session, min_size_bytes=50_000, clump_size_bytes=2_000_000, max_size_bytes=8_000_000, compression_enabled=compress
    )
    if encrypted:
        set_encryption_key(db_session)
    set_encryption_enabled(db_session, encrypted)
    rng = random.Random(7)
    files = {}
    for i in range(n):
        size = rng.randint(80_000, 200_000) if i % 10 == 0 else rng.randint(300, 20_000)  # ~10% "single", rest clump
        path = tmp_path / "src" / f"d{i % 25}" / f"f{i}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rng.randbytes(size))
        files[i] = catalog.make_media_file(
            path=str(path), filename=path.name, storage_location_id=source.id, size_bytes=size
        )
    return source, destination, files


def _run(source, destination, backend):
    return _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)


@pytest.mark.parametrize("encrypted", [False, True])
def test_resync_of_an_already_backed_up_library_touches_nothing(db_session, catalog, tmp_path, encrypted):
    source, destination, files = _build_library(db_session, catalog, tmp_path, N, encrypted=encrypted)
    backend = CountingBackend(LocalBackend(tmp_path / "bucket"))

    first = _run(source, destination, backend)
    assert first["status"] == "ok" and first["files"] == N
    print(f"\n[first backup] files={N} archives={first['archives']} ops={dict(backend.ops)}")

    backend.reset()
    with _Queries() as q:
        again = _run(source, destination, backend)
    print(f"[re-sync, nothing changed] ops={backend.total_ops} db_queries={q.count} skipped={len(again['skipped'])}")
    assert again["files"] == 0 and again["archives"] == 0
    assert all(s["reason"] == "unchanged" for s in again["skipped"]) and len(again["skipped"]) == N
    # one batched bucket listing for the whole run, and nothing else
    assert backend.total_ops == 1 and backend.ops.get("list") == 1 and backend.bytes_up == 0
    assert q.count < 20 + N // 10  # DB work must not be per-file


def test_only_edited_files_are_reuploaded(db_session, catalog, tmp_path):
    source, destination, files = _build_library(db_session, catalog, tmp_path, N, encrypted=True)
    backend = CountingBackend(LocalBackend(tmp_path / "bucket"))
    _run(source, destination, backend)
    first_bytes = backend.bytes_up

    edited = list(range(0, N, 50))  # 2%
    edited_bytes = 0
    for i in edited:
        p = tmp_path / "src" / f"d{i % 25}" / f"f{i}.bin"
        p.write_bytes(p.read_bytes() + b"edit")
        edited_bytes += p.stat().st_size
        os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5_000_000_000))
        db_session.execute(__import__("sqlalchemy").text("update media_files set size_bytes=:s where id=:i"), {"s": p.stat().st_size, "i": files[i].id})
    db_session.commit()

    backend.reset()
    result = _run(source, destination, backend)
    print(f"[2% edited] files uploaded={result['files']} ops={dict(backend.ops)} bytes_up={backend.bytes_up} of first={first_bytes}")
    assert result["files"] == len(edited)
    assert backend.bytes_up < edited_bytes * 1.5 + 100_000  # the edited files (plus tar/blob overhead), nothing else
    assert backend.bytes_up < first_bytes * 0.25


def test_renamed_or_copied_files_are_not_reuploaded(db_session, catalog, tmp_path):
    """Same content under a new path (a move, a rename, a second copy) is
    already in the bucket - it should be recorded, not uploaded again."""
    source, destination, files = _build_library(db_session, catalog, tmp_path, N, encrypted=True)
    backend = CountingBackend(LocalBackend(tmp_path / "bucket"))
    _run(source, destination, backend)
    first_bytes = backend.bytes_up

    copies = list(range(0, N, 20))  # 5%: new paths, identical bytes
    for i in copies:
        src = tmp_path / "src" / f"d{i % 25}" / f"f{i}.bin"
        dst = tmp_path / "src" / "copies" / f"copy{i}.bin"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        catalog.make_media_file(path=str(dst), filename=dst.name, storage_location_id=source.id, size_bytes=dst.stat().st_size)

    backend.reset()
    result = _run(source, destination, backend)
    print(f"[5% copies] files={result['files']} ops={dict(backend.ops)} bytes_up={backend.bytes_up} of first={first_bytes}")
    assert result["files"] == 0 and result["adopted"] == len(copies)  # all recorded, none uploaded
    # one batched bucket listing for the whole run, and nothing else
    assert backend.total_ops == 1 and backend.ops.get("list") == 1 and backend.bytes_up == 0
    assert result["status"] == "ok"  # adopting is not a problem with the run

    for i in copies[:5]:  # a copy restores byte-identical from the shared stored bytes
        dst = tmp_path / "src" / "copies" / f"copy{i}.bin"
        media_file = db_session.query(MediaFile).filter_by(path=str(dst)).one()
        record, parts = _v2_ledger_parts(db_session, media_file.id, destination.id)
        assert record.sha256 and record.status == "done"
        out = tmp_path / "restored" / dst.name
        _restore_v2(db_session, media_file, record, parts, out, backend)
        assert out.read_bytes() == dst.read_bytes()

    backend.reset()
    again = _run(source, destination, backend)  # and the next sync sees them as unchanged
    assert again["files"] == 0 and again["adopted"] == 0 and backend.total_ops == 1 and backend.ops.get("list") == 1


@pytest.mark.parametrize("encrypted", [False, True])
def test_shallow_verify_cost_tracks_archives_not_files(db_session, catalog, tmp_path, encrypted):
    source, destination, files = _build_library(db_session, catalog, tmp_path, N, encrypted=encrypted)
    backend = CountingBackend(LocalBackend(tmp_path / "bucket"))
    _run(source, destination, backend)
    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).count()
    stored_bytes = sum(a.size_bytes for a in db_session.query(BackupArchive).filter_by(storage_location_id=destination.id))

    backend.reset()
    results = _verify_v2_batch(db_session, [f.id for f in files.values()], destination.id, backend)
    print(
        f"\n[shallow verify, encrypted={encrypted}] files={N} archives={archives} "
        f"{backend.summary()} ({backend.total_ops / archives:.1f}/archive, {backend.total_ops / N:.2f}/file)"
    )
    assert set(results.values()) == {"match"} and len(results) == N
    assert backend.total_ops <= 3 * archives + 5  # per archive: one metadata, one index, one data read - at most
    assert backend.bytes_down < 1.5 * stored_bytes  # each stored byte read about once, not once per file
