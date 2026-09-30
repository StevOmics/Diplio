"""BackupRun tracking, file-subset runs, and the generated test set end to end
(docs/backup-plan/steps/11-run-tracking.md). Database-backed
(MEDIABRIDGE_TEST_DB=1) - scratch DB only, see docs/backup-plan/HANDOFF.md."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from app.backup_run import BACKUP_RUN_LOCK_KEY, BackupRunRefused, _execute_backup_run
from app.db import engine
from app.models import BackupArchive, BackupRun
from app.tasks import _restore_v2, _v2_ledger_parts
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

REPO_ROOT = Path(__file__).resolve().parents[2]


def _new_run(db_session, source, destination, scope="library", file_ids=None) -> BackupRun:
    run = BackupRun(
        scope=scope,
        source_storage_location_id=source.id,
        destination_storage_location_id=destination.id,
        file_ids=json.dumps(file_ids) if file_ids is not None else None,
    )
    db_session.add(run)
    db_session.commit()
    return run


def _files(db_session, catalog, tmp_path, contents: dict[str, bytes]):
    source = catalog.make_storage_location(path=str(tmp_path))
    mfs = {}
    for name, data in contents.items():
        write_file(tmp_path, name, data)
        mfs[name] = catalog.make_media_file(
            path=str(tmp_path / name), filename=name, storage_location_id=source.id, size_bytes=len(data)
        )
    return source, mfs


def test_run_row_tracks_a_successful_run(db_session, catalog, tmp_path):
    source, mfs = _files(db_session, catalog, tmp_path, {"a.txt": b"a" * 100, "b.txt": b"b" * 200})
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_encryption_key(db_session)
    set_transfer_config(db_session, min_size_bytes=1000, clump_size_bytes=100_000, max_size_bytes=2 * 1024 * 1024)
    run = _new_run(db_session, source, destination)

    result = _execute_backup_run(
        source.id, destination.id, run_id=run.id, backend=LocalBackend(tmp_path / "bucket"), upload_sleep=lambda s: None
    )
    assert result["status"] == "ok"

    db_session.expire_all()
    run = db_session.get(BackupRun, run.id)
    assert run.status == "done"
    assert run.encrypted is True
    assert run.files_total == 2 and run.archives_total == run.archives_done == 1
    assert run.bytes_uploaded > 0
    assert run.started_at and run.completed_at and run.detail is None and run.error_message is None


def test_refusal_is_recorded_on_the_run(db_session, catalog, tmp_path):
    source, _ = _files(db_session, catalog, tmp_path, {"a.txt": b"a"})
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_encryption_enabled(db_session, True)
    from app.models import BackupEncryptionConfig

    db_session.query(BackupEncryptionConfig).first().password = None
    db_session.commit()
    run = _new_run(db_session, source, destination)

    with pytest.raises(BackupRunRefused):
        _execute_backup_run(source.id, destination.id, run_id=run.id, backend=LocalBackend(tmp_path / "bucket"))

    db_session.expire_all()
    run = db_session.get(BackupRun, run.id)
    assert run.status == "failed"
    assert "password" in run.error_message
    assert run.completed_at is not None


def test_file_subset_backs_up_only_those_files(db_session, catalog, tmp_path):
    source, mfs = _files(db_session, catalog, tmp_path, {"a.txt": b"a" * 100, "b.txt": b"b" * 200, "c.txt": b"c" * 300})
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_encryption_enabled(db_session, False)
    set_transfer_config(db_session, min_size_bytes=1000, clump_size_bytes=100_000, max_size_bytes=2 * 1024 * 1024)
    chosen = [mfs["a.txt"].id, mfs["c.txt"].id]
    run = _new_run(db_session, source, destination, scope="selection", file_ids=chosen)

    _execute_backup_run(
        source.id,
        destination.id,
        file_ids=chosen,
        run_id=run.id,
        backend=LocalBackend(tmp_path / "bucket"),
        upload_sleep=lambda s: None,
    )

    assert _v2_ledger_parts(db_session, mfs["a.txt"].id, destination.id) is not None
    assert _v2_ledger_parts(db_session, mfs["c.txt"].id, destination.id) is not None
    assert _v2_ledger_parts(db_session, mfs["b.txt"].id, destination.id) is None
    db_session.expire_all()
    assert db_session.get(BackupRun, run.id).files_total == 2


def test_busy_lock_leaves_run_queued(db_session, catalog, tmp_path):
    source, _ = _files(db_session, catalog, tmp_path, {"a.txt": b"a"})
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_encryption_enabled(db_session, False)
    run = _new_run(db_session, source, destination)

    holder = engine.connect()
    try:
        assert holder.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": BACKUP_RUN_LOCK_KEY}).scalar()
        result = _execute_backup_run(source.id, destination.id, run_id=run.id, backend=LocalBackend(tmp_path / "bucket"))
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BACKUP_RUN_LOCK_KEY})
        holder.close()

    assert result["status"] == "skipped"
    db_session.expire_all()
    run = db_session.get(BackupRun, run.id)
    assert run.status == "queued" and "Waiting" in run.detail


def _load_generator():
    if not (REPO_ROOT / "scripts" / "make-test-media.py").exists():
        pytest.skip("scripts/ not available (repo root not mounted)")
    spec = importlib.util.spec_from_file_location("make_test_media", REPO_ROOT / "scripts" / "make-test-media.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_set_backs_up_and_restores_at_ui_minimum_max_size(db_session, catalog, tmp_path):
    """The generator's set, encrypted, at the smallest max_size the Settings
    page allows (64 MiB, real encryption chunking): every archive type shows
    up, nothing exceeds max_size, and every file restores byte-identically."""
    gen = _load_generator()
    root = tmp_path / "set"
    root.mkdir()
    (root / gen.MARKER).write_text("x")
    made = gen.build(root, seed=7, mid_count=2, large_mib=[70], tiny_count=8)

    source = catalog.make_storage_location(path=str(root))
    mfs = {}
    for rel, size, _digest in made:
        mfs[rel] = catalog.make_media_file(
            path=str(root / rel), filename=Path(rel).name, storage_location_id=source.id, size_bytes=size
        )
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_encryption_key(db_session)
    max_size = 64 * 1024 * 1024
    set_transfer_config(db_session, min_size_bytes=1024 * 1024, clump_size_bytes=64 * 1024 * 1024, max_size_bytes=max_size)
    run = _new_run(db_session, source, destination)

    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, run_id=run.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"

    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).all()
    assert {a.archive_type for a in archives} == {"clump", "single", "part"}
    assert all(a.size_bytes <= max_size for a in archives)

    # Unchanged files are skipped on a second run.
    run2 = _new_run(db_session, source, destination)
    again = _execute_backup_run(source.id, destination.id, run_id=run2.id, backend=backend, upload_sleep=lambda s: None)
    assert again["archives"] == 0 and again["files"] == 0

    for rel, size, digest in made:
        mf = mfs[rel]
        record, parts = _v2_ledger_parts(db_session, mf.id, destination.id)
        out = tmp_path / "restored" / rel
        _restore_v2(db_session, mf, record, parts, out, backend)
        assert out.stat().st_size == size, rel
        assert out.read_bytes() == (root / rel).read_bytes(), rel


def test_objects_land_under_archive_path_plus_library_subfolder(db_session, catalog, tmp_path):
    source, mfs = _files(db_session, catalog, tmp_path, {"a.txt": b"a" * 100})
    destination = catalog.make_storage_location(
        location_type="gcs", is_backup_target=True, path=f"gs://my-bucket/folder1-{os.getpid()}"
    )
    source.archive_location_id = destination.id
    source.archive_subpath = "movies/2024"
    db_session.commit()
    set_cloud_config(db_session)
    set_encryption_enabled(db_session, False)
    set_transfer_config(db_session, min_size_bytes=1000, clump_size_bytes=100_000, max_size_bytes=2 * 1024 * 1024)

    _execute_backup_run(source.id, destination.id, backend=LocalBackend(tmp_path / "bucket"), upload_sleep=lambda s: None)

    base = f"folder1-{os.getpid()}/movies/2024"
    keys = sorted(str(p.relative_to(tmp_path / "bucket")) for p in (tmp_path / "bucket").rglob("*") if p.is_file())
    assert keys and all(k.startswith(base + "/") for k in keys), keys
    assert any("/archives/" in k and k.endswith(".tar") for k in keys)
    assert any("/index/" in k and k.endswith(".json") for k in keys)


def test_subfolder_ignored_when_library_is_assigned_elsewhere(db_session, catalog, tmp_path):
    source, _ = _files(db_session, catalog, tmp_path, {"a.txt": b"a" * 100})
    other = catalog.make_storage_location(location_type="gcs", is_backup_target=True, path=f"gs://other-bucket-{os.getpid()}")
    destination = catalog.make_storage_location(location_type="gcs", is_backup_target=True, path=f"gs://my-bucket-{os.getpid()}/x")
    source.archive_location_id = other.id
    source.archive_subpath = "should/not/apply"
    db_session.commit()
    set_cloud_config(db_session)
    set_encryption_enabled(db_session, False)
    set_transfer_config(db_session, min_size_bytes=1000, clump_size_bytes=100_000, max_size_bytes=2 * 1024 * 1024)

    _execute_backup_run(source.id, destination.id, backend=LocalBackend(tmp_path / "bucket"), upload_sleep=lambda s: None)

    keys = [str(p.relative_to(tmp_path / "bucket")) for p in (tmp_path / "bucket").rglob("*") if p.is_file()]
    assert keys and all(k.startswith("x/") and "should" not in k for k in keys), keys
