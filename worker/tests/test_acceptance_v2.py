"""Acceptance tests for the v2 backup pipeline (docs/cfa-spec.md section 9).
See docs/backup-plan/steps/09-acceptance-tests.md.

No new production code here - steps 06-08 already implement everything
section 9 checks. These tests exercise the real pipeline end to end
(backup_run._execute_backup_run + tasks._restore_v2) together, rather than
re-testing any one module in isolation, and shell out to the real `tar`/`cat`
binaries where section 9 explicitly names them, not tarfile/Python
concatenation - the point of those two bullets is interoperability with real
tools, not just self-consistency with our own reader.

Database-backed (MEDIABRIDGE_TEST_DB=1); no test contacts a real bucket.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile

import pytest

from app.backup_run import _execute_backup_run
from app.models import BackupArchive
from app.tasks import _restore_v2, _v2_ledger_parts
from tests.conftest import (
    set_cloud_config,
    set_encryption_enabled,
    set_transfer_config,
    usable_destination,
    write_file,
)
from tests.storage_double import FlakyBackend, LocalBackend

pytestmark = pytest.mark.usefixtures(
    "encryption_config_state", "cloud_storage_config_state", "transfer_config_state"
)

MIN_SIZE = 1_000
MAX_SIZE = 2_000_000
# D1's resolution (DEVIATIONS.md): the single/part boundary is max_size less
# a 1 MiB tar-overhead margin, not max_size itself.
PAYLOAD_CEILING = MAX_SIZE - 1024 * 1024


def _backup(db_session, source, destination, backend):
    set_encryption_enabled(db_session, False)
    set_cloud_config(db_session)
    set_transfer_config(db_session, min_size_bytes=MIN_SIZE, clump_size_bytes=100_000, max_size_bytes=MAX_SIZE)
    return _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)


def _write_size_matrix(tmp_path, catalog, source):
    """The four-point size matrix section 9 names: 0 bytes, just under
    min_size (clump), just over min_size (single), and over the payload
    ceiling (a 2-part split) - all comfortably under max_size."""
    contents = {
        "zero.bin": b"",
        "under_min.bin": os.urandom(MIN_SIZE - 1),
        "over_min.bin": os.urandom(MIN_SIZE + 1),
        "over_ceiling.bin": os.urandom(PAYLOAD_CEILING + 100_000),
    }
    matrix = {}
    for name, data in contents.items():
        write_file(tmp_path, name, data)
        media_file = catalog.make_media_file(
            path=str(tmp_path / name), filename=name, storage_location_id=source.id, size_bytes=len(data)
        )
        matrix[name] = (media_file, data)
    return matrix


# --- "Round trip: files of 0 bytes, just under and just over min_size, and
# over max_size restore byte-identical." ------------------------------------


def test_round_trip_size_matrix_byte_identical(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    matrix = _write_size_matrix(tmp_path, catalog, source)

    backend = LocalBackend(tmp_path / "bucket")
    result = _backup(db_session, source, destination, backend)
    assert result["status"] == "ok"
    assert result["files"] == len(matrix)

    for name, (media_file, data) in matrix.items():
        v2 = _v2_ledger_parts(db_session, media_file.id, destination.id)
        assert v2 is not None, name
        record, parts = v2
        output_path = tmp_path / "restored" / name
        _restore_v2(db_session, media_file, record, parts, output_path, backend)

        assert output_path.read_bytes() == data, name


# --- "No uploaded object exceeds max_size." ---------------------------------


def test_no_uploaded_object_exceeds_max_size(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    _write_size_matrix(tmp_path, catalog, source)

    backend = LocalBackend(tmp_path / "bucket")
    result = _backup(db_session, source, destination, backend)
    assert result["status"] == "ok"

    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).all()
    assert archives
    for archive in archives:
        assert backend.stat(archive.path).size <= MAX_SIZE, archive.path


# --- "Archives extract with tar -xf; split files reassemble with cat." -----


def test_archives_extract_with_real_tar_and_split_files_reassemble_with_cat(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    matrix = _write_size_matrix(tmp_path, catalog, source)

    backend = LocalBackend(tmp_path / "bucket")
    result = _backup(db_session, source, destination, backend)
    assert result["status"] == "ok"

    for name in ("zero.bin", "under_min.bin", "over_min.bin"):
        media_file, data = matrix[name]
        v2 = _v2_ledger_parts(db_session, media_file.id, destination.id)
        assert v2 is not None, name
        _, parts = v2
        assert len(parts) == 1, name
        _, archive = parts[0]

        extract_dir = tmp_path / f"extract-{name}"
        extract_dir.mkdir()
        subprocess.run(
            ["tar", "-xf", str(backend.root / archive.path), "-C", str(extract_dir)],
            check=True,
            capture_output=True,
        )
        extracted = [p for p in extract_dir.rglob("*") if p.is_file() and p.name == name]
        assert len(extracted) == 1, name
        assert extracted[0].read_bytes() == data, name

    # The split file: each part extracts on its own with real tar -xf, and
    # `cat`-ing them back together in order reproduces the original bytes.
    media_file, data = matrix["over_ceiling.bin"]
    v2 = _v2_ledger_parts(db_session, media_file.id, destination.id)
    assert v2 is not None
    _, parts = v2
    assert len(parts) == 2

    part_files = []
    for i, (_, archive) in enumerate(parts):
        extract_dir = tmp_path / f"extract-part-{i}"
        extract_dir.mkdir()
        subprocess.run(
            ["tar", "-xf", str(backend.root / archive.path), "-C", str(extract_dir)],
            check=True,
            capture_output=True,
        )
        extracted = [p for p in extract_dir.rglob("*") if p.is_file()]
        assert len(extracted) == 1
        part_files.append(extracted[0])

    combined_path = tmp_path / "combined.bin"
    with combined_path.open("wb") as out:
        subprocess.run(["cat", *[str(p) for p in part_files]], stdout=out, check=True)

    assert combined_path.read_bytes() == data


# --- "Every index entry's hash, offset, and size match the archive." -------


def test_index_entries_match_archive_bytes(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    matrix = _write_size_matrix(tmp_path, catalog, source)

    backend = LocalBackend(tmp_path / "bucket")
    result = _backup(db_session, source, destination, backend)
    assert result["status"] == "ok"

    archives = db_session.query(BackupArchive).filter_by(storage_location_id=destination.id).all()
    assert archives

    hash_to_data = {hashlib.sha256(data).hexdigest(): data for _, data in matrix.values()}
    part_bytes_by_hash: dict[str, dict[int, bytes]] = {}

    for archive in archives:
        index_key = archive.path.replace("archives/", "index/", 1).replace(".tar", ".json")
        index = json.loads((backend.root / index_key).read_bytes())
        archive_bytes = (backend.root / archive.path).read_bytes()

        for file_hash, entry in index["files"].items():
            if "part" not in entry:
                # clump/single: a ranged read at [offset, offset+size) *is*
                # the plain file's bytes - no tar parsing needed.
                member_bytes = archive_bytes[entry["offset"] : entry["offset"] + entry["size"]]
                assert hashlib.sha256(member_bytes).hexdigest() == file_hash
                assert member_bytes == hash_to_data[file_hash]
            else:
                # part: `size` is the whole file's size (section 5), not
                # this member's byte length - extract via tarfile, and only
                # check the whole-file hash once every part sharing this
                # hash has been collected, below.
                with tarfile.open(backend.root / archive.path) as tf:
                    members = tf.getmembers()
                    assert len(members) == 1
                    extracted = tf.extractfile(members[0]).read()
                part_bytes_by_hash.setdefault(file_hash, {})[entry["part"]] = extracted
                assert entry["size"] == len(hash_to_data[file_hash])

    for file_hash, parts_by_index in part_bytes_by_hash.items():
        combined = b"".join(parts_by_index[i] for i in sorted(parts_by_index))
        assert hashlib.sha256(combined).hexdigest() == file_hash
        assert combined == hash_to_data[file_hash]


# --- "An interrupted run leaves no index file without a complete archive." -


def test_interrupted_run_leaves_no_index_without_a_complete_archive(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    set_encryption_enabled(db_session, False)
    set_cloud_config(db_session)
    set_transfer_config(db_session, min_size_bytes=MIN_SIZE, clump_size_bytes=100_000, max_size_bytes=MAX_SIZE)

    # Two distinct "single" files, named so they pack (and upload) in this
    # order: a_first, then z_second.
    content_a = os.urandom(MIN_SIZE + 100)
    content_b = os.urandom(MIN_SIZE + 200)
    write_file(tmp_path, "a_first.bin", content_a)
    write_file(tmp_path, "z_second.bin", content_b)
    catalog.make_media_file(
        path=str(tmp_path / "a_first.bin"), filename="a_first.bin", storage_location_id=source.id, size_bytes=len(content_a)
    )
    catalog.make_media_file(
        path=str(tmp_path / "z_second.bin"),
        filename="z_second.bin",
        storage_location_id=source.id,
        size_bytes=len(content_b),
    )

    inner = LocalBackend(tmp_path / "bucket")
    # Call #1 (a_first's only upload attempt) succeeds; calls #2-#6
    # (z_second's five retry attempts) all fail, exhausting retries and
    # aborting the run (DEVIATIONS.md D6) after a_first is fully recorded.
    backend = FlakyBackend(inner, fail_uploads_on={2, 3, 4, 5, 6}, exc=RuntimeError("simulated network failure"))

    with pytest.raises(Exception):
        _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)

    index_dir = tmp_path / "bucket" / "index"
    archives_dir = tmp_path / "bucket" / "archives"
    index_files = list(index_dir.glob("*.json")) if index_dir.exists() else []
    assert index_files, "expected a_first's index to have been written before the interruption"

    for index_path in index_files:
        archive_path = archives_dir / index_path.name.replace(".json", ".tar")
        assert archive_path.exists(), f"{index_path} has no matching archive"
        # Real `tar -tf` (list, not extract) succeeds only against a
        # non-corrupt, non-truncated archive.
        subprocess.run(["tar", "-tf", str(archive_path)], check=True, capture_output=True)


# --- "An unchanged file is not uploaded again." -----------------------------


def test_unchanged_file_is_not_uploaded_again(db_session, catalog, tmp_path):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = usable_destination(catalog)
    write_file(tmp_path, "a.txt", b"hello world")
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=11
    )

    backend = LocalBackend(tmp_path / "bucket")
    first = _backup(db_session, source, destination, backend)
    assert first["status"] == "ok"

    bucket_root = tmp_path / "bucket"
    before = {p.relative_to(bucket_root): p.read_bytes() for p in bucket_root.rglob("*") if p.is_file()}

    second = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert second["status"] == "ok"
    assert second["files"] == 0

    after = {p.relative_to(bucket_root): p.read_bytes() for p in bucket_root.rglob("*") if p.is_file()}
    assert after == before
