"""Batched verify: one verdict per file identical to the per-file check, but
with cloud cost that tracks archives, not files. DB-backed (scratch DB)."""
from __future__ import annotations

import os

import pytest

from app.backup_run import _execute_backup_run
from app.models import BackupArchive
from app.tasks import (
    VERIFY_RANGE_MERGE_GAP,
    _RangeCache,
    _v2_ledger_parts,
    _verify_v2,
    _verify_v2_batch,
)
from tests.conftest import set_cloud_config, set_encryption_enabled, set_encryption_key, set_transfer_config, usable_destination
from tests.storage_double import CountingBackend, LocalBackend


# --- _RangeCache (pure) ---------------------------------------------------------


class _Blob:
    def __init__(self, data: bytes):
        self.data, self.reads = data, []

    def read_range(self, key, offset, length):
        self.reads.append((key, offset, length))
        return self.data[offset : offset + length]


def test_range_cache_merges_nearby_ranges_into_one_request():
    blob = _Blob(bytes(range(256)) * 4096)  # 1 MiB
    cache = _RangeCache(blob)
    for start in (0, 1000, 2000, 3000):  # four wanted slices, all near each other
        cache.want("k", start, 100)
    cache.prefetch()
    assert len(blob.reads) == 1 and blob.reads[0] == ("k", 0, 3100)
    n = len(blob.reads)
    assert cache.read_range("k", 2000, 100) == blob.data[2000:2100]
    assert len(blob.reads) == n  # served from memory


def test_range_cache_does_not_merge_across_a_big_gap_and_falls_through_on_a_miss():
    blob = _Blob(os.urandom(4 * VERIFY_RANGE_MERGE_GAP))
    cache = _RangeCache(blob)
    cache.want("k", 0, 100)
    cache.want("k", 3 * VERIFY_RANGE_MERGE_GAP, 100)
    cache.prefetch()
    assert len(blob.reads) == 2  # two separate requests, not one giant one
    n = len(blob.reads)
    assert cache.read_range("k", 500, 10) == blob.data[500:510]  # not prefetched -> direct read
    assert len(blob.reads) == n + 1


def test_range_cache_clear_drops_memory():
    blob = _Blob(b"x" * 1000)
    cache = _RangeCache(blob)
    cache.want("k", 0, 1000)
    cache.prefetch()
    cache.clear()
    cache.read_range("k", 0, 10)
    assert len(blob.reads) == 2  # had to read again


# --- batches --------------------------------------------------------------------

pytestmark_db = pytest.mark.usefixtures("encryption_config_state", "cloud_storage_config_state", "transfer_config_state")


def _library(db_session, catalog, tmp_path, n, *, encrypted, min_size=50_000, clump=2_000_000):
    source = catalog.make_storage_location(path=str(tmp_path / "src"))
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    set_transfer_config(db_session, min_size_bytes=min_size, clump_size_bytes=clump, max_size_bytes=8_000_000)
    if encrypted:
        set_encryption_key(db_session)
    set_encryption_enabled(db_session, encrypted)
    files = []
    for i in range(n):
        p = tmp_path / "src" / f"f{i}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(500 + i))  # small: they clump
        files.append(catalog.make_media_file(path=str(p), filename=p.name, storage_location_id=source.id, size_bytes=p.stat().st_size))
    backend = CountingBackend(LocalBackend(tmp_path / "bucket"))
    assert _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)["status"] == "ok"
    backend.reset()
    return destination, files, backend


@pytestmark_db
@pytest.mark.parametrize("encrypted", [False, True])
def test_a_whole_clump_is_verified_with_a_handful_of_requests(db_session, catalog, tmp_path, encrypted):
    destination, files, backend = _library(db_session, catalog, tmp_path, 60, encrypted=encrypted)
    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).count()
    assert archives == 1  # 60 small files, one clump

    results = _verify_v2_batch(db_session, [f.id for f in files], destination.id, backend)
    assert set(results.values()) == {"match"}
    # one metadata stat + one index download + one merged data read - for 60 files
    assert backend.ops == {"stat": 1, "read_object": 1, "read_range": 1}


@pytestmark_db
def test_per_file_verify_of_the_same_clump_costs_far_more(db_session, catalog, tmp_path):
    """The old shape of the cost, kept as a contrast: no shared context."""
    destination, files, backend = _library(db_session, catalog, tmp_path, 30, encrypted=False)
    for f in files:
        record, parts = _v2_ledger_parts(db_session, f.id, destination.id)
        assert _verify_v2(db_session, f, record, parts, backend) == "match"
    assert backend.total_ops >= 30 * 3


@pytestmark_db
def test_batch_verdicts_match_the_per_file_verdicts_including_damage(db_session, catalog, tmp_path):
    destination, files, backend = _library(db_session, catalog, tmp_path, 20, encrypted=True)
    (tmp_path / "src" / "f3.bin").write_bytes(b"edited later")  # local changed
    os.utime(tmp_path / "src" / "f3.bin", ns=(1, 1))
    (tmp_path / "src" / "f7.bin").unlink()  # local gone: bucket copy still fine

    archive = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).first()
    stored = backend._inner._path(archive.path)
    raw = bytearray(stored.read_bytes())
    raw[-3] ^= 0xFF  # damage the archive (crc32c now differs) - every file in it is affected
    stored.write_bytes(bytes(raw))

    batch = _verify_v2_batch(db_session, [f.id for f in files], destination.id, backend)
    for f in files:
        record, parts = _v2_ledger_parts(db_session, f.id, destination.id)
        assert batch[f.id] == _verify_v2(db_session, f, record, parts, backend)
    assert set(batch.values()) == {"mismatch"}


@pytestmark_db
def test_batch_reports_missing_index_and_missing_archive(db_session, catalog, tmp_path):
    destination, files, backend = _library(db_session, catalog, tmp_path, 10, encrypted=False)
    archive = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).one()
    backend._inner._path(archive.path.replace("archives/", "index/").replace(".tar", ".json")).unlink()
    assert set(_verify_v2_batch(db_session, [f.id for f in files], destination.id, backend).values()) == {"missing"}

    backend._inner._path(archive.path).unlink()
    assert set(_verify_v2_batch(db_session, [f.id for f in files], destination.id, backend).values()) == {"missing"}


@pytestmark_db
def test_many_archives_are_checked_by_listing_few_by_stat(db_session, catalog, tmp_path):
    # min_size low + big-ish files -> one "single" archive each
    destination, files, backend = _library(db_session, catalog, tmp_path, 12, encrypted=False, min_size=100, clump=1)
    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).count()
    assert archives >= 10

    # all of them wanted: listing (1 request per 1000 objects) beats a stat each
    _verify_v2_batch(db_session, [f.id for f in files], destination.id, backend)
    assert backend.ops.get("list") == 1 and "stat" not in backend.ops

    # just one wanted out of a library this small: one stat is cheaper than a listing
    backend.reset()
    _verify_v2_batch(db_session, [files[0].id], destination.id, backend)
    assert backend.ops.get("stat") == 1 and "list" not in backend.ops


@pytestmark_db
def test_batch_writes_results_to_the_records(db_session, catalog, tmp_path):
    destination, files, backend = _library(db_session, catalog, tmp_path, 5, encrypted=False)
    _verify_v2_batch(db_session, [f.id for f in files], destination.id, backend)
    db_session.expire_all()
    for f in files:
        record, _ = _v2_ledger_parts(db_session, f.id, destination.id)
        assert record.verify_status == "match" and record.verified_at is not None


@pytestmark_db
def test_files_without_a_v2_backup_are_ignored(db_session, catalog, tmp_path):
    destination, files, backend = _library(db_session, catalog, tmp_path, 3, encrypted=False)
    stray = catalog.make_media_file(path=str(tmp_path / "x"), filename="x", storage_location_id=files[0].storage_location_id, size_bytes=1)
    results = _verify_v2_batch(db_session, [stray.id, files[0].id, 999_999_999], destination.id, backend)
    assert list(results) == [files[0].id]
