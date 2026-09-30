"""bucket_inventory.discover(): reading a backup's index files directly out of
a bucket, with no database row to work from (portability, step 18). Pure -
LocalBackend only, no MEDIABRIDGE_TEST_DB needed. See test_sync_run.py for
the real-backup-then-discover integration test."""
from __future__ import annotations

import json

from app.bucket_inventory import discover, list_index_keys

from tests.storage_double import LocalBackend


def _plain_resolver(sha256, entry, archive_id):
    return entry.get("paths")


def _write_index(backend, key, *, archive_id, object_key, files, archive_type="clump", encrypted=False, archive_bytes=b"x" * 100):
    backend.write_bytes(object_key, archive_bytes)
    index = {"v": 1, "archive_id": archive_id, "object": object_key, "type": archive_type, "created_at": "2026-01-02T03:04:05Z", "size": len(archive_bytes), "files": files}
    if encrypted:
        index["encrypted"] = True
    backend.write_bytes(key, json.dumps(index).encode())


def test_finds_a_simple_plaintext_file(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    _write_index(
        backend, "test/a.json", archive_id="a", object_key="test/a.tar",
        files={"h1": {"paths": ["movies/foo.mp4"], "size": 500, "mtime": "2026-01-02T03:04:05Z", "member": "movies/foo.mp4", "offset": 512}},
    )
    result = discover(backend, "test/", _plain_resolver)
    assert len(result.files) == 1
    f = result.files[0]
    assert f.sha256 == "h1" and f.paths == ["movies/foo.mp4"] and f.size_bytes == 500
    assert len(f.parts) == 1
    part = f.parts[0]
    assert (part.archive_path, part.archive_id, part.archive_type, part.offset, part.size) == ("test/a.tar", "a", "clump", 512, 500)
    assert not result.bad_index_keys and not result.dangling_index_keys


def test_list_index_keys_ignores_non_json_objects(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    backend.write_bytes("test/a.tar", b"x")
    backend.write_bytes("test/a.json", b"{}")
    backend.write_bytes("test/notes.txt", b"hi")
    assert list_index_keys(backend, "test/") == ["test/a.json"]


def test_a_file_shared_across_two_index_files_merges_paths_and_parts(tmp_path):
    # A file backed up twice from two different paths ends up in two
    # different archives (e.g. two separate runs) - both must surface.
    backend = LocalBackend(tmp_path / "bucket")
    _write_index(backend, "test/a.json", archive_id="a", object_key="test/a.tar",
                 files={"h1": {"paths": ["orig/foo.mp4"], "size": 500, "mtime": "2026-01-02T03:04:05Z", "member": "m", "offset": 0}})
    _write_index(backend, "test/b.json", archive_id="b", object_key="test/b.tar",
                 files={"h1": {"paths": ["renamed/foo.mp4"], "size": 500, "mtime": "2026-01-02T03:04:05Z", "member": "m", "offset": 0}})
    result = discover(backend, "test/", _plain_resolver)
    assert len(result.files) == 1
    f = result.files[0]
    assert len(f.parts) == 2
    assert {p.archive_id for p in f.parts} == {"a", "b"}
    # The resolver's first non-empty answer wins (both agree here anyway).
    assert f.paths == ["orig/foo.mp4"]


def test_split_file_parts_are_ordered_by_part_number(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    for part in (2, 1):  # written out of order - discover() must still sort them
        _write_index(
            backend, f"test/p{part}.json", archive_id=f"p{part}", object_key=f"test/p{part}.tar", archive_type="part",
            files={"h1": {"size": 9_000_000, "mtime": "2026-01-02T03:04:05Z", "member": "m", "part": part, "parts": 2}},
        )
    result = discover(backend, "test/", lambda sha, entry, aid: ["big.bin"])
    assert len(result.files) == 1
    assert [p.part for p in result.files[0].parts] == [1, 2]
    assert [p.archive_id for p in result.files[0].parts] == ["p1", "p2"]


def test_unresolvable_paths_report_none_but_still_surface_the_content(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    _write_index(
        backend, "test/a.json", archive_id="a", object_key="test/a.tar", encrypted=True,
        files={"h1": {"paths_enc": "sealed-token", "size": 500, "mtime": "2026-01-02T03:04:05Z", "member": "h1.file", "offset": 0}},
    )
    result = discover(backend, "test/", lambda sha, entry, aid: None)  # can't decrypt
    assert len(result.files) == 1
    assert result.files[0].paths is None
    assert result.files[0].parts[0].encrypted is True


def test_bad_json_index_is_reported_not_raised(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    backend.write_bytes("test/a.tar", b"x")
    backend.write_bytes("test/a.json", b"{not valid json")
    result = discover(backend, "test/", _plain_resolver)
    assert result.files == [] and result.bad_index_keys == ["test/a.json"]


def test_index_missing_required_fields_is_reported_not_raised(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    backend.write_bytes("test/a.tar", b"x")
    backend.write_bytes("test/a.json", json.dumps({"v": 1, "files": {}}).encode())  # no object/archive_id
    result = discover(backend, "test/", _plain_resolver)
    assert result.bad_index_keys == ["test/a.json"]


def test_index_whose_archive_object_is_missing_is_dangling(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    index = {"v": 1, "archive_id": "a", "object": "test/a.tar", "type": "clump", "created_at": "2026-01-02T03:04:05Z", "size": 1, "files": {}}
    backend.write_bytes("test/a.json", json.dumps(index).encode())  # a.tar was never written
    result = discover(backend, "test/", _plain_resolver)
    assert result.files == [] and result.dangling_index_keys == ["test/a.json"]


def test_disagreeing_entries_for_one_hash_are_dropped_not_guessed_at(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    _write_index(backend, "test/a.json", archive_id="a", object_key="test/a.tar",
                 files={"h1": {"paths": ["x"], "size": 500, "mtime": "2026-01-02T03:04:05Z", "member": "m", "offset": 0}})
    _write_index(backend, "test/b.json", archive_id="b", object_key="test/b.tar",
                 files={"h1": {"paths": ["y"], "size": 999, "mtime": "2026-01-02T03:04:05Z", "member": "m", "offset": 0}})  # different size, same hash
    result = discover(backend, "test/", _plain_resolver)
    assert result.files == []


def test_mtime_is_parsed_to_nanoseconds(tmp_path):
    from datetime import datetime, timezone

    backend = LocalBackend(tmp_path / "bucket")
    _write_index(backend, "test/a.json", archive_id="a", object_key="test/a.tar",
                 files={"h1": {"paths": ["x"], "size": 500, "mtime": "2026-01-02T03:04:05Z", "member": "m", "offset": 0}})
    result = discover(backend, "test/", _plain_resolver)
    expected = int(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    assert result.files[0].mtime_ns == expected


def test_resolver_is_called_with_the_archive_id_so_callers_can_cache_by_archive(tmp_path):
    backend = LocalBackend(tmp_path / "bucket")
    _write_index(backend, "test/a.json", archive_id="my-archive-id", object_key="test/a.tar",
                 files={"h1": {"paths": ["x"], "size": 500, "mtime": "2026-01-02T03:04:05Z", "member": "m", "offset": 0}})
    seen = []
    discover(backend, "test/", lambda sha, entry, aid: (seen.append(aid), entry.get("paths"))[1])
    assert seen == ["my-archive-id"]
