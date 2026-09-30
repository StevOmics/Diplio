"""_run_progress_view: how a BackupRun row becomes the live-progress payload
the Backup Runs page polls. Pure - no database."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.main import RUN_STALL_SECONDS, _run_progress_view

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


def _run(**kw):
    base = dict(
        id=1, status="running", phase="uploading", phase_done=0, phase_total=0,
        phase_started_at=NOW - timedelta(seconds=10), heartbeat_at=NOW - timedelta(seconds=1),
        started_at=NOW - timedelta(seconds=30), detail=None, error_message=None,
        archives_done=0, archives_total=2, bytes_uploaded=0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_upload_percent_rate_and_eta():
    v = _run_progress_view(_run(phase_done=250, phase_total=1000), NOW)
    assert v["percent"] == 25.0 and v["unit"] == "bytes" and v["label"] == "Uploading"
    assert v["rate"] == 25.0  # 250 bytes over the phase's 10 s
    assert v["eta_seconds"] == 30.0  # 750 left at 25/s
    assert v["active"] and not v["stalled"]


def test_files_phases_count_files():
    v = _run_progress_view(_run(phase="hashing", phase_done=3, phase_total=12), NOW)
    assert v["unit"] == "files" and v["percent"] == 25.0 and v["label"] == "Hashing files"


def test_no_rate_or_eta_in_the_first_seconds_or_before_any_progress():
    early = _run_progress_view(_run(phase_done=500, phase_total=1000, phase_started_at=NOW - timedelta(seconds=1)), NOW)
    assert early["rate"] is None and early["eta_seconds"] is None
    nothing = _run_progress_view(_run(phase_done=0, phase_total=1000), NOW)
    assert nothing["rate"] is None and nothing["percent"] == 0.0


def test_unknown_total_has_no_percent():
    v = _run_progress_view(_run(phase="packing", phase_total=0), NOW)
    assert v["percent"] is None and v["eta_seconds"] is None and v["label"] == "Packing archives"


def test_percent_is_capped_at_100():
    assert _run_progress_view(_run(phase_done=1200, phase_total=1000), NOW)["percent"] == 100.0


def test_stale_heartbeat_is_reported_as_stalled():
    fresh = _run_progress_view(_run(heartbeat_at=NOW - timedelta(seconds=RUN_STALL_SECONDS - 1)), NOW)
    assert not fresh["stalled"]
    stuck = _run_progress_view(_run(heartbeat_at=NOW - timedelta(seconds=RUN_STALL_SECONDS + 5)), NOW)
    assert stuck["stalled"] and stuck["heartbeat_age"] > RUN_STALL_SECONDS


def test_a_run_that_never_beat_falls_back_to_its_start_time():
    v = _run_progress_view(_run(heartbeat_at=None, started_at=NOW - timedelta(seconds=500)), NOW)
    assert v["stalled"]


def test_queued_and_finished_runs_carry_no_live_progress():
    q = _run_progress_view(_run(status="queued", phase=None, started_at=None, heartbeat_at=None), NOW)
    assert q["active"] and q["phase"] is None and q["heartbeat_age"] is None and not q["stalled"]
    done = _run_progress_view(_run(status="done", phase="uploading", phase_done=5, phase_total=5), NOW)
    assert not done["active"] and done["phase"] is None and done["percent"] is None and not done["stalled"]


def test_restore_runs_share_the_view_but_have_their_own_key_and_byte_progress():
    run = _run(phase="restoring", phase_done=300, phase_total=1200)
    del run.archives_done, run.archives_total, run.bytes_uploaded  # a RestoreRun has none of these
    v = _run_progress_view(run, NOW, kind="restore")
    assert v["key"] == "restore-1" and v["unit"] == "bytes" and v["label"] == "Restoring files"
    assert v["percent"] == 25.0 and v["archives_total"] == 0 and v["bytes_uploaded"] == 0


def test_backup_keys_are_namespaced_too():
    assert _run_progress_view(_run(), NOW)["key"] == "backup-1"
