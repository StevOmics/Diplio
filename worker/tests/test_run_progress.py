"""Live progress reporting for backup runs: the upload progress callback, the
throttled _Progress writer, and a full run's phase sequence. The first groups
need no database; the last is DB-backed (MEDIABRIDGE_TEST_DB=1, scratch DB)."""
from __future__ import annotations

import io
import os
from datetime import datetime

import pytest

from app import backup_run
from app.backup_run import _execute_backup_run, _Progress
from app.gcs import _ProgressReader
from app.models import BackupRun
from app.storage import upload_and_confirm
from tests.conftest import (
    set_cloud_config,
    set_encryption_enabled,
    set_encryption_key,
    set_transfer_config,
    usable_destination,
    write_file,
)
from tests.storage_double import FlakyBackend, LocalBackend


# --- gcs._ProgressReader ---------------------------------------------------------


def test_progress_reader_reports_position_after_each_read():
    seen = []
    reader = _ProgressReader(io.BytesIO(b"x" * 100), seen.append)
    assert len(reader.read(30)) == 30
    assert len(reader.read(50)) == 50
    assert reader.read(50) == b"x" * 20
    assert reader.read(50) == b""  # nothing read, nothing reported
    assert seen == [30, 80, 100]


def test_progress_reader_is_honest_when_the_uploader_seeks_back_to_retry():
    seen = []
    reader = _ProgressReader(io.BytesIO(b"x" * 100), seen.append)
    reader.read(60)
    reader.seek(20)  # a resumable upload retrying a chunk
    reader.read(30)
    assert seen == [60, 50]  # went back; did not double-count to 90


# --- upload_and_confirm ----------------------------------------------------------


def test_upload_and_confirm_reports_progress_and_checksum_done(tmp_path):
    f = tmp_path / "a.tar"
    f.write_bytes(os.urandom(4000))
    events = []
    upload_and_confirm(
        LocalBackend(tmp_path / "bucket"),
        "k",
        f,
        progress_cb=lambda n: events.append(("sent", n)),
        on_checksummed=lambda: events.append(("checksummed", None)),
    )
    assert events[0] == ("checksummed", None)  # before any bytes go out
    sent = [n for kind, n in events if kind == "sent"]
    assert sent == sorted(sent) and sent[-1] == 4000


def test_upload_and_confirm_progress_restarts_on_retry(tmp_path):
    f = tmp_path / "a.tar"
    f.write_bytes(os.urandom(4000))
    sent = []
    backend = FlakyBackend(LocalBackend(tmp_path / "bucket"), fail_uploads_on={1}, exc=RuntimeError("network blip"))
    upload_and_confirm(backend, "k", f, sleep=lambda s: None, progress_cb=sent.append)
    assert sent[-1] == 4000 and backend.upload_calls == 2  # the failed first attempt never reported


def test_upload_and_confirm_without_callbacks_is_unchanged(tmp_path):
    f = tmp_path / "a.tar"
    f.write_bytes(b"hello")
    assert upload_and_confirm(LocalBackend(tmp_path / "bucket"), "k", f).size == 5


# --- _Progress ------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def writes(monkeypatch):
    log = []
    monkeypatch.setattr(backup_run, "_update_run", lambda db, run_id, **fields: log.append(fields))
    return log


def test_phase_change_writes_immediately_and_resets_counters(writes):
    p = _Progress(None, 1, clock=_Clock())
    p.phase("uploading", 5000)
    assert writes[-1]["phase"] == "uploading" and writes[-1]["phase_total"] == 5000 and writes[-1]["phase_done"] == 0
    assert isinstance(writes[-1]["phase_started_at"], datetime) and writes[-1]["heartbeat_at"]


def test_advance_is_throttled_but_flush_always_writes(writes):
    clock = _Clock()
    p = _Progress(None, 1, min_interval=1.0, clock=clock)
    p.phase("uploading", 1000)
    n = len(writes)
    for done in range(1, 50):  # a burst of callbacks inside one second
        clock.t += 0.01
        p.advance(done)
    assert len(writes) == n  # all coalesced
    clock.t += 1.0
    p.advance(60)
    assert len(writes) == n + 1 and writes[-1]["phase_done"] == 60
    p.advance(70)
    p.flush()
    assert writes[-1]["phase_done"] == 70  # flush is never throttled


def test_finish_clears_the_phase(writes):
    p = _Progress(None, 1, clock=_Clock())
    p.phase("packing")
    p.finish()
    assert writes[-1]["phase"] is None and writes[-1]["phase_total"] == 0


def test_progress_with_no_run_id_writes_nothing():
    p = _Progress(None, None)  # the real _update_run: returns immediately for run_id None
    p.phase("hashing", 3)
    p.advance(2)
    p.flush()
    p.finish()


# --- a whole run (DB-backed) -----------------------------------------------------

db_tests = pytest.mark.usefixtures("encryption_config_state", "cloud_storage_config_state", "transfer_config_state")


@db_tests
@pytest.mark.parametrize("encrypted", [False, True])
def test_run_reports_each_phase_in_order_and_ends_clean(db_session, catalog, tmp_path, encrypted, monkeypatch):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_transfer_config(db_session, min_size_bytes=100, clump_size_bytes=100_000_000, max_size_bytes=8_000_000)
    if encrypted:
        set_encryption_key(db_session)
    set_encryption_enabled(db_session, encrypted)
    for name in ("a.txt", "b.txt", "c.txt"):
        data = os.urandom(3000)
        write_file(tmp_path, name, data)
        catalog.make_media_file(path=str(tmp_path / name), filename=name, storage_location_id=source.id, size_bytes=len(data))
    run = BackupRun(scope="library", source_storage_location_id=source.id, destination_storage_location_id=destination.id)
    db_session.add(run)
    db_session.commit()

    phases, real = [], backup_run._update_run

    def spy(db, run_id, **fields):
        if "phase" in fields and fields["phase"] is not None:
            phases.append((fields["phase"], fields.get("phase_total")))
        return real(db, run_id, **fields)

    monkeypatch.setattr(backup_run, "_update_run", spy)
    result = _execute_backup_run(source.id, destination.id, run_id=run.id, backend=LocalBackend(tmp_path / "bucket"), upload_sleep=lambda s: None)
    assert result["status"] == "ok"

    names = [p for p, _ in phases]
    assert names == (["hashing", "preparing", "packing", "uploading"] if encrypted else ["hashing", "packing", "uploading"])
    assert dict(phases)["hashing"] == 3  # total = files being considered
    assert dict(phases)["uploading"] > 0  # total = bytes of the archives to send

    db_session.expire_all()
    row = db_session.get(BackupRun, run.id)
    assert row.status == "done" and row.phase is None and row.phase_total == 0
    assert row.heartbeat_at is not None and row.detail is None
