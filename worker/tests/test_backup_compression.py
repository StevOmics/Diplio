"""Database-backed tests for optional gzip compression in the v2 pipeline
(docs/backup-plan/steps/14-compression.md): real backup runs against a
LocalBackend, then restore and verify. MEDIABRIDGE_TEST_DB=1 - and per
CLAUDE.md, a scratch database, never the dev one."""
from __future__ import annotations

import json
import os

import pytest

from app.backup_index import index_object_key
from app.backup_run import _execute_backup_run
from app.models import BackupArchive
from app.tasks import _restore_v2, _v2_ledger_parts, _verify_v2
from tests.conftest import (
    set_cloud_config,
    set_encryption_enabled,
    set_encryption_key,
    set_transfer_config,
    usable_destination,
)
from tests.storage_double import LocalBackend

pytestmark = pytest.mark.usefixtures(
    "encryption_config_state", "cloud_storage_config_state", "transfer_config_state"
)

TEXT = b"the quick brown fox jumps over the lazy dog\n" * 4000  # ~172 KB, compresses ~100x


def _setup(db_session, catalog, tmp_path, files: dict[str, bytes], *, encrypted: bool, compress: bool = True, **transfer):
    source = catalog.make_storage_location(path=str(tmp_path / "src"))
    destination = usable_destination(catalog)
    set_cloud_config(db_session)
    settings = dict(min_size_bytes=100, clump_size_bytes=100_000_000, max_size_bytes=8_000_000, compression_enabled=compress)
    settings.update(transfer)
    set_transfer_config(db_session, **settings)
    if encrypted:
        set_encryption_key(db_session)
    set_encryption_enabled(db_session, encrypted)
    media_files = {}
    for name, data in files.items():
        path = tmp_path / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        media_files[name] = catalog.make_media_file(
            path=str(path), filename=name, storage_location_id=source.id, size_bytes=len(data)
        )
    backend = LocalBackend(tmp_path / "bucket")
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"
    return destination, media_files, backend


def _restore(db_session, destination, media_file, backend, out):
    record, parts = _v2_ledger_parts(db_session, media_file.id, destination.id)
    _restore_v2(db_session, media_file, record, parts, out, backend)
    return out.read_bytes()


@pytest.mark.parametrize("encrypted", [False, True])
def test_round_trip_and_record_marks_compression(db_session, catalog, tmp_path, encrypted):
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"notes.txt": TEXT}, encrypted=encrypted)
    record, parts = _v2_ledger_parts(db_session, mfs["notes.txt"].id, destination.id)
    assert record.compression == "gzip"
    stored = sum(a.size_bytes for _, a in parts)
    assert stored < len(TEXT) / 5  # actually smaller in the bucket
    assert _restore(db_session, destination, mfs["notes.txt"], backend, tmp_path / "out.txt") == TEXT


def test_plain_member_is_named_dot_gz_and_is_valid_gzip(db_session, catalog, tmp_path):
    import gzip
    import tarfile

    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"docs/notes.txt": TEXT}, encrypted=False)
    _, parts = _v2_ledger_parts(db_session, mfs["docs/notes.txt"].id, destination.id)
    archive = parts[0][1]
    with tarfile.open(backend._path(archive.path)) as tf:
        names = tf.getnames()
        assert names == ["docs/notes.txt.gz"]
        assert gzip.decompress(tf.extractfile(names[0]).read()) == TEXT  # `tar -xf` then `gunzip` works


def test_encrypted_member_name_stays_opaque(db_session, catalog, tmp_path):
    import tarfile

    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"docs/notes.txt": TEXT}, encrypted=True)
    _, parts = _v2_ledger_parts(db_session, mfs["docs/notes.txt"].id, destination.id)
    with tarfile.open(backend._path(parts[0][1].path)) as tf:
        assert all(n.endswith(".file") and "notes" not in n for n in tf.getnames())


@pytest.mark.parametrize("encrypted", [False, True])
def test_index_entry_records_compression(db_session, catalog, tmp_path, encrypted):
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"a.txt": TEXT, "b.mp4": os.urandom(5000)}, encrypted=encrypted)
    entries = []
    for archive in db_session.query(BackupArchive).filter_by(storage_location_id=destination.id):
        idx = backend._path(index_object_key(archive.archive_id, archive.path[: archive.path.rfind("archives/")]))
        entries += json.loads(idx.read_text())["files"].values()
    by_compression = sorted(e.get("compression") or "none" for e in entries)
    assert by_compression == ["gzip", "none"]


@pytest.mark.parametrize("encrypted", [False, True])
def test_incompressible_and_known_compressed_files_are_stored_as_is(db_session, catalog, tmp_path, encrypted):
    files = {"movie.mp4": TEXT, "random.bin": os.urandom(50_000)}  # .mp4 wins by extension; .bin by sampling
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, files, encrypted=encrypted)
    for name, data in files.items():
        record, _ = _v2_ledger_parts(db_session, mfs[name].id, destination.id)
        assert record.compression is None
        assert _restore(db_session, destination, mfs[name], backend, tmp_path / f"out-{name}") == data


@pytest.mark.parametrize("encrypted", [False, True])
def test_mixed_clump_restores_each_file_byte_identical(db_session, catalog, tmp_path, encrypted):
    files = {"a.txt": TEXT, "b.txt": TEXT[:3000] + b"different", "c.bin": os.urandom(4000), "d.txt": b"tiny"}
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, files, encrypted=encrypted, min_size_bytes=1_000_000)
    assert [a.archive_type for a in db_session.query(BackupArchive).filter_by(storage_location_id=destination.id)] == ["clump"]
    for name, data in files.items():
        assert _restore(db_session, destination, mfs[name], backend, tmp_path / f"out-{name}") == data


@pytest.mark.parametrize("encrypted", [False, True])
def test_large_compressed_file_that_still_needs_splitting(db_session, catalog, tmp_path, encrypted):
    # hex of random bytes compresses ~2:1, so this still gzips to over max_size.
    data = os.urandom(10_000_000).hex().encode()
    destination, mfs, backend = _setup(
        db_session, catalog, tmp_path, {"big.txt": data}, encrypted=encrypted, max_size_bytes=8_000_000
    )
    record, parts = _v2_ledger_parts(db_session, mfs["big.txt"].id, destination.id)
    assert record.compression == "gzip"
    assert len(parts) >= 2
    assert all(a.archive_type == "part" and a.size_bytes <= 8_000_000 for _, a in parts)
    assert _restore(db_session, destination, mfs["big.txt"], backend, tmp_path / "big.out") == data


def test_compression_off_writes_no_compression(db_session, catalog, tmp_path):
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=False, compress=False)
    record, parts = _v2_ledger_parts(db_session, mfs["a.txt"].id, destination.id)
    assert record.compression is None
    assert parts[0][1].size_bytes > len(TEXT)  # a plain tar of the whole file


def test_toggling_setting_later_does_not_break_restore_of_old_backup(db_session, catalog, tmp_path):
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=False, compress=True)
    set_transfer_config(db_session, compression_enabled=False)  # restore must follow the record, not the setting
    assert _restore(db_session, destination, mfs["a.txt"], backend, tmp_path / "out") == TEXT


def test_corrupt_gzip_stream_fails_restore_without_touching_output(db_session, catalog, tmp_path):
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=False)
    _, parts = _v2_ledger_parts(db_session, mfs["a.txt"].id, destination.id)
    stored = backend._path(parts[0][1].path)
    data = bytearray(stored.read_bytes())
    data[700] ^= 0xFF  # inside the gzip payload, past the tar header
    stored.write_bytes(bytes(data))
    out = tmp_path / "out.txt"
    with pytest.raises(RuntimeError):
        _restore(db_session, destination, mfs["a.txt"], backend, out)
    assert not out.exists() and not list(tmp_path.glob("out.txt.*"))


# --- verify: sampled (default) and deep, over compressed backups --------------


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("deep", [False, True])
def test_verify_compressed_backup_matches(db_session, catalog, tmp_path, encrypted, deep):
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=encrypted)
    record, parts = _v2_ledger_parts(db_session, mfs["a.txt"].id, destination.id)
    assert _verify_v2(db_session, mfs["a.txt"], record, parts, backend, deep=deep) == "match"


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("deep", [False, True])
def test_verify_flags_local_edit_as_changed(db_session, catalog, tmp_path, encrypted, deep):
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"a.txt": TEXT}, encrypted=encrypted)
    record, parts = _v2_ledger_parts(db_session, mfs["a.txt"].id, destination.id)
    (tmp_path / "src" / "a.txt").write_bytes(TEXT + b"edited")
    assert _verify_v2(db_session, mfs["a.txt"], record, parts, backend, deep=deep) == "changed"


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("where", ["head", "tail"])
def test_sampled_verify_catches_damage_at_either_end_without_the_crc(db_session, catalog, tmp_path, encrypted, compress, where):
    """Blank the recorded CRC32C so only the sampling logic can notice."""
    data = TEXT if compress else os.urandom(20_000)
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"a.txt": data}, encrypted=encrypted, compress=compress)
    record, parts = _v2_ledger_parts(db_session, mfs["a.txt"].id, destination.id)
    link, archive = parts[0]
    archive.crc32c = None
    stored = backend._path(archive.path)
    raw = bytearray(stored.read_bytes())
    pos = link.archive_offset + 12 if where == "head" else link.archive_offset + link.archive_length - 5
    raw[pos] ^= 0xFF
    stored.write_bytes(bytes(raw))
    assert _verify_v2(db_session, mfs["a.txt"], record, parts, backend) == "mismatch"


def test_deep_verify_catches_corrupt_middle_that_sampling_cannot_see(db_session, catalog, tmp_path):
    """The documented limit of sampling, and why deep exists."""
    data = os.urandom(3_000_000)
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"big.bin": data}, encrypted=False, compress=False)
    record, parts = _v2_ledger_parts(db_session, mfs["big.bin"].id, destination.id)
    link, archive = parts[0]
    archive.crc32c = None  # simulate a bug that wrote bad bytes and recorded their CRC
    stored = backend._path(archive.path)
    raw = bytearray(stored.read_bytes())
    raw[link.archive_offset + 1_500_000] ^= 0xFF
    stored.write_bytes(bytes(raw))
    assert _verify_v2(db_session, mfs["big.bin"], record, parts, backend) == "match"
    assert _verify_v2(db_session, mfs["big.bin"], record, parts, backend, deep=True) == "mismatch"


@pytest.mark.parametrize("encrypted", [False, True])
def test_shallow_verify_of_a_big_split_file_never_downloads_it(db_session, catalog, tmp_path, encrypted):
    data = os.urandom(10_000_000).hex().encode()
    destination, mfs, backend = _setup(db_session, catalog, tmp_path, {"big.txt": data}, encrypted=encrypted, max_size_bytes=8_000_000)
    record, parts = _v2_ledger_parts(db_session, mfs["big.txt"].id, destination.id)
    assert len(parts) >= 2

    reads = []
    real = backend.read_range
    backend.read_range = lambda key, off, n: (reads.append(n), real(key, off, n))[1]
    assert _verify_v2(db_session, mfs["big.txt"], record, parts, backend) == "match"
    if encrypted:
        # A big encrypted file's smallest checkable unit is a 64 MiB chunk - too
        # dear for a shallow verify, which leans on the object CRC32C + index.
        assert reads == []
    else:
        assert reads and max(reads) <= 1024 * 1024  # just the two 1 MiB ends
