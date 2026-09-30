"""Restore runs: tracked, batched restores back to original paths
(worker/app/restore_run.py). DB-backed - scratch DB only (see CLAUDE.md)."""
from __future__ import annotations

import json
import os

import pytest

from app import restore_run as restore_module
from app.backup_run import _execute_backup_run
from app.models import RestoreRun
from app.restore_run import _execute_restore_run
from app.tasks import _v2_ledger_parts
from tests.conftest import (
    set_cloud_config,
    set_encryption_enabled,
    set_encryption_key,
    set_transfer_config,
    usable_destination,
)
from tests.storage_double import LocalBackend

pytestmark = pytest.mark.usefixtures("encryption_config_state", "cloud_storage_config_state", "transfer_config_state")

TEXT = b"the quick brown fox jumps over the lazy dog\n" * 3000


def _library(db_session, catalog, tmp_path, files: dict[str, bytes], *, encrypted, compress=False):
    source = catalog.make_storage_location(path=str(tmp_path / "src"))
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_transfer_config(db_session, min_size_bytes=100, clump_size_bytes=100_000_000, max_size_bytes=8_000_000, compression_enabled=compress)
    if encrypted:
        set_encryption_key(db_session)
    set_encryption_enabled(db_session, encrypted)
    mfs = {}
    for name, data in files.items():
        path = tmp_path / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        mfs[name] = catalog.make_media_file(path=str(path), filename=path.name, storage_location_id=source.id, size_bytes=len(data))
    backend = LocalBackend(tmp_path / "bucket")
    assert _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)["status"] == "ok"
    return source, destination, mfs, backend


def _new_run(db_session, destination, media_files, scope="selection", mode="replace_older") -> RestoreRun:
    run = RestoreRun(
        destination_storage_location_id=destination.id,
        scope=scope,
        file_ids=json.dumps([m.id for m in media_files]),
        mode=mode,
    )
    db_session.add(run)
    db_session.commit()
    return run


def _row(db_session, run) -> RestoreRun:
    db_session.expire_all()
    return db_session.get(RestoreRun, run.id)


@pytest.mark.parametrize("encrypted,compress", [(False, False), (True, False), (False, True), (True, True)])
def test_restores_files_over_missing_originals_and_reports_done(db_session, catalog, tmp_path, encrypted, compress):
    files = {"a.txt": TEXT, "b/c.bin": os.urandom(5000), "d.txt": TEXT[:777]}
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, files, encrypted=encrypted, compress=compress)
    for name in files:
        (tmp_path / "src" / name).unlink()  # lost the local copies
    run = _new_run(db_session, destination, list(mfs.values()))

    result = _execute_restore_run(run.id, backend=backend)

    assert result["status"] == "done" and result["restored"] == 3 and result["failed"] == 0
    for name, data in files.items():
        assert (tmp_path / "src" / name).read_bytes() == data
    row = _row(db_session, run)
    assert (row.status, row.files_total, row.files_done, row.files_failed) == ("done", 3, 3, 0)
    assert row.bytes_restored == sum(len(d) for d in files.values())
    assert row.phase is None and row.detail is None and row.completed_at and row.started_at
    assert sorted(r["status"] for r in json.loads(row.results_json)) == ["restored"] * 3


def test_restore_overwrites_an_edited_local_file_with_the_backed_up_version(db_session, catalog, tmp_path):
    # mode="replace_all": today's unconditional-overwrite behavior, regardless
    # of the edited local file's (now newer) mtime.
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=True)
    (tmp_path / "src" / "a.txt").write_bytes(b"corrupted since the backup")
    run = _new_run(db_session, destination, [mfs["a.txt"]], scope="file", mode="replace_all")
    assert _execute_restore_run(run.id, backend=backend)["status"] == "done"
    assert (tmp_path / "src" / "a.txt").read_bytes() == TEXT


def test_a_file_without_a_backup_makes_the_run_partial_not_failed(db_session, catalog, tmp_path):
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=False)
    stray = catalog.make_media_file(path=str(tmp_path / "src" / "never_backed_up.txt"), filename="never_backed_up.txt", storage_location_id=mfs["a.txt"].storage_location_id, size_bytes=5)
    (tmp_path / "src" / "a.txt").unlink()
    run = _new_run(db_session, destination, [mfs["a.txt"], stray])

    result = _execute_restore_run(run.id, backend=backend)

    assert result["status"] == "partial" and (result["restored"], result["failed"]) == (1, 1)
    assert (tmp_path / "src" / "a.txt").read_bytes() == TEXT  # the good one still came back
    failed = [r for r in json.loads(_row(db_session, run).results_json) if r["status"] == "failed"]
    assert failed[0]["path"].endswith("never_backed_up.txt") and "no completed backup" in failed[0]["error"]


@pytest.mark.parametrize("encrypted", [False, True])
def test_a_tampered_archive_fails_that_file_and_leaves_the_existing_file_alone(db_session, catalog, tmp_path, encrypted):
    files = {"good.txt": TEXT, "bad.bin": os.urandom(6000)}
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, files, encrypted=encrypted)
    # bad.bin lives in its own single archive when min_size is low enough; tamper with whichever archive holds it
    _, parts = _v2_ledger_parts(db_session, mfs["bad.bin"].id, destination.id)
    link, archive = parts[0]
    stored = backend._path(archive.path)
    raw = bytearray(stored.read_bytes())
    raw[link.archive_offset + 30] ^= 0xFF
    stored.write_bytes(bytes(raw))
    (tmp_path / "src" / "bad.bin").write_bytes(b"what is on disk now")
    (tmp_path / "src" / "good.txt").unlink()
    # replace_all: this test is about tamper detection, not the mode feature -
    # force the attempt regardless of bad.bin's now-newer local mtime.
    run = _new_run(db_session, destination, list(mfs.values()), mode="replace_all")

    result = _execute_restore_run(run.id, backend=backend)

    tampered_shared_archive = _v2_ledger_parts(db_session, mfs["good.txt"].id, destination.id)[1][0][1].id == archive.id
    if tampered_shared_archive:
        # both files live in one clump: the damage may hit either or both, but never silently
        assert result["failed"] >= 1
    else:
        assert result["status"] == "partial" and (tmp_path / "src" / "good.txt").read_bytes() == TEXT
    assert (tmp_path / "src" / "bad.bin").read_bytes() == b"what is on disk now"  # verify-before-place
    assert not list((tmp_path / "src").glob("*.mbcopy"))


def test_progress_phases_and_heartbeat_are_reported(db_session, catalog, tmp_path, monkeypatch):
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, {"a.txt": TEXT, "b.txt": TEXT[:900]}, encrypted=True)
    # replace_all: local files are still present with the mtime they were
    # backed up at, which replace_older's default would otherwise skip.
    run = _new_run(db_session, destination, list(mfs.values()), mode="replace_all")

    seen, real = [], restore_module._update_restore_run

    def spy(db, run_id, **fields):
        seen.append(dict(fields))
        return real(db, run_id, **fields)

    monkeypatch.setattr(restore_module, "_update_restore_run", spy)
    _execute_restore_run(run.id, backend=backend)

    phases = [f for f in seen if f.get("phase")]
    assert phases and phases[0]["phase"] == "restoring" and phases[0]["phase_total"] == len(TEXT) + 900
    assert any("Restoring 1/2" in (f.get("detail") or "") for f in seen)
    assert any(f.get("heartbeat_at") for f in seen)
    row = _row(db_session, run)
    assert row.phase is None and row.status == "done"


def test_an_unusable_setup_fails_the_run_with_a_message(db_session, catalog, tmp_path, monkeypatch):
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=False)
    run = _new_run(db_session, destination, [mfs["a.txt"]])
    from app.models import CloudStorageConfig

    db_session.query(CloudStorageConfig).delete()
    db_session.commit()

    result = _execute_restore_run(run.id)  # no injected backend -> must build one -> no cloud config

    assert result["status"] == "failed"
    row = _row(db_session, run)
    assert row.status == "failed" and "Google Cloud service account" in row.error_message and row.completed_at


def test_an_unknown_run_is_a_no_op():
    assert _execute_restore_run(999_999_999)["status"] == "missing"


# --- mode: replace_older / replace_all ---------------------------------------


def test_replace_older_skips_a_file_newer_locally_than_the_backup(db_session, catalog, tmp_path):
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=False)
    local_path = tmp_path / "src" / "a.txt"
    # Local file edited after the backup - now newer than the recorded mtime.
    local_path.write_bytes(b"edited locally after the backup")
    os.utime(local_path, ns=(local_path.stat().st_atime_ns, local_path.stat().st_mtime_ns + 10_000_000_000))
    run = _new_run(db_session, destination, [mfs["a.txt"]], scope="file", mode="replace_older")

    result = _execute_restore_run(run.id, backend=backend)

    assert result["status"] == "done"
    assert local_path.read_bytes() == b"edited locally after the backup"  # untouched
    row = _row(db_session, run)
    results = json.loads(row.results_json)
    assert results[0]["status"] == "skipped" and "newer" in results[0]["reason"]
    assert row.status == "done"  # a skip must not count as partial/failed


def test_replace_all_overwrites_regardless_of_local_mtime(db_session, catalog, tmp_path):
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=False)
    local_path = tmp_path / "src" / "a.txt"
    local_path.write_bytes(b"edited locally after the backup")
    os.utime(local_path, ns=(local_path.stat().st_atime_ns, local_path.stat().st_mtime_ns + 10_000_000_000))
    run = _new_run(db_session, destination, [mfs["a.txt"]], scope="file", mode="replace_all")

    result = _execute_restore_run(run.id, backend=backend)

    assert result["status"] == "done" and result["restored"] == 1
    assert local_path.read_bytes() == TEXT


def test_replace_older_restores_when_no_local_file_exists(db_session, catalog, tmp_path):
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=False)
    (tmp_path / "src" / "a.txt").unlink()
    run = _new_run(db_session, destination, [mfs["a.txt"]], scope="file", mode="replace_older")

    result = _execute_restore_run(run.id, backend=backend)

    assert result["status"] == "done" and result["restored"] == 1
    assert (tmp_path / "src" / "a.txt").read_bytes() == TEXT


def test_replace_older_restores_when_local_file_is_older(db_session, catalog, tmp_path):
    _, destination, mfs, backend = _library(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=False)
    local_path = tmp_path / "src" / "a.txt"
    local_path.write_bytes(b"stale local copy, older than the backup")
    os.utime(local_path, ns=(local_path.stat().st_atime_ns, local_path.stat().st_mtime_ns - 10_000_000_000))
    run = _new_run(db_session, destination, [mfs["a.txt"]], scope="file", mode="replace_older")

    result = _execute_restore_run(run.id, backend=backend)

    assert result["status"] == "done" and result["restored"] == 1
    assert local_path.read_bytes() == TEXT
