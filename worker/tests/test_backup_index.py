import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backup_index import (
    INDEX_VERSION,
    archive_object_key,
    build_index,
    index_object_key,
    rfc3339,
)
from app.packer import FileToPack, PackedArchive, PackedMember, pack

# Small, deliberately artificial thresholds, mirroring test_packer.py, so
# tests don't need multi-hundred-MB files.
MIN_SIZE = 200
CLUMP_SIZE = 32 * 1024
MIB = 1024 * 1024
MAX_SIZE = 3 * MIB
CHUNK = MAX_SIZE - MIB

CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _pack(files, dest_dir, **overrides):
    kwargs = dict(min_size=MIN_SIZE, clump_size=CLUMP_SIZE, max_size=MAX_SIZE)
    kwargs.update(overrides)
    return pack(files, dest_dir=dest_dir, **kwargs)


def _make_file(tmp_path: Path, name: str, data: bytes, mtime_ns: int = 1_700_000_000_123_456_789) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    import os

    os.utime(p, ns=(mtime_ns, mtime_ns))
    return p


def _file_to_pack(
    source_root: Path, rel_path: str, data: bytes, sha256: str | None = None, content_id: str | None = None
) -> FileToPack:
    import hashlib

    path = _make_file(source_root, rel_path, data)
    return FileToPack(
        source_path=path,
        rel_path=rel_path,
        sha256=sha256 or hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        mtime_ns=path.stat().st_mtime_ns,
        content_id=content_id,
    )


def _pack_one_clump(tmp_path: Path) -> PackedArchive:
    source_root = tmp_path / "clump-src"
    dest_dir = tmp_path / "clump-dest"
    dest_dir.mkdir()
    f = _file_to_pack(source_root, "photos/2024/img001.jpg", b"x" * 100)
    (archive,) = _pack([f], dest_dir)
    return archive


def _pack_one_single(tmp_path: Path) -> PackedArchive:
    source_root = tmp_path / "single-src"
    dest_dir = tmp_path / "single-dest"
    dest_dir.mkdir()
    f = _file_to_pack(source_root, "movies/big.mkv", b"y" * MIN_SIZE)
    (archive,) = _pack([f], dest_dir)
    return archive


def _pack_parts(tmp_path: Path) -> list[PackedArchive]:
    source_root = tmp_path / "parts-src"
    dest_dir = tmp_path / "parts-dest"
    dest_dir.mkdir()
    f = _file_to_pack(source_root, "movies/huge.mkv", b"z" * (CHUNK + 1))
    archives = _pack([f], dest_dir)
    assert len(archives) > 1
    return archives


# --- Shape, one per archive type --------------------------------------------


def test_clump_index_matches_spec_shape(tmp_path):
    archive = _pack_one_clump(tmp_path)
    idx = build_index(archive, prefix="server01/", created_at=CREATED_AT)

    member = archive.members[0]
    expected = {
        "v": INDEX_VERSION,
        "archive_id": archive.archive_id,
        "object": f"server01/archives/{archive.archive_id}.tar",
        "type": "clump",
        "created_at": "2026-01-02T03:04:05Z",
        "size": archive.size_bytes,
        "files": {
            member.sha256: {
                "paths": ["photos/2024/img001.jpg"],
                "size": member.size_bytes,
                "mtime": rfc3339(datetime.fromtimestamp(member.mtime_ns // 1_000_000_000, tz=timezone.utc)),
                "member": "photos/2024/img001.jpg",
                "offset": member.offset,
            }
        },
    }
    assert idx == expected


def test_single_index_matches_spec_shape(tmp_path):
    archive = _pack_one_single(tmp_path)
    idx = build_index(archive, prefix="server01/", created_at=CREATED_AT)

    member = archive.members[0]
    expected = {
        "v": INDEX_VERSION,
        "archive_id": archive.archive_id,
        "object": f"server01/archives/{archive.archive_id}.tar",
        "type": "single",
        "created_at": "2026-01-02T03:04:05Z",
        "size": archive.size_bytes,
        "files": {
            member.sha256: {
                "paths": ["movies/big.mkv"],
                "size": member.size_bytes,
                "mtime": rfc3339(datetime.fromtimestamp(member.mtime_ns // 1_000_000_000, tz=timezone.utc)),
                "member": "movies/big.mkv",
                "offset": member.offset,
            }
        },
    }
    assert idx == expected


def test_part_index_carries_part_and_parts(tmp_path):
    archives = _pack_parts(tmp_path)
    for archive in archives:
        idx = build_index(archive, prefix=None, created_at=CREATED_AT)
        member = archive.members[0]
        entry = idx["files"][member.sha256]
        assert entry["part"] == member.part
        assert entry["parts"] == member.part_count
        assert entry["part"] >= 1


# --- The three details settled with Steve -----------------------------------


def test_part_entry_size_is_whole_file_not_part_size(tmp_path):
    archives = _pack_parts(tmp_path)
    whole_file_size = CHUNK + 1
    for archive in archives:
        member = archive.members[0]
        idx = build_index(archive, prefix=None, created_at=CREATED_AT)
        entry = idx["files"][member.sha256]
        assert entry["size"] == whole_file_size
        assert entry["size"] != archive.size_bytes  # the tar is smaller than the whole file


def test_mtime_is_rfc3339_utc_seconds(tmp_path):
    source_root = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    # 1700000000.123456789 -> truncated to whole seconds
    f = _file_to_pack(source_root, "a.txt", b"hello", sha256=None)
    (archive,) = _pack([f], dest_dir)
    idx = build_index(archive, prefix=None, created_at=CREATED_AT)
    member = archive.members[0]
    entry = idx["files"][member.sha256]
    expected = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert entry["mtime"] == expected


def test_created_at_is_injected_not_clock(tmp_path):
    archive = _pack_one_single(tmp_path)
    idx1 = build_index(archive, prefix="server01/", created_at=CREATED_AT)
    idx2 = build_index(archive, prefix="server01/", created_at=CREATED_AT)
    assert idx1 == idx2


# --- Keys and paths ----------------------------------------------------------


def test_files_keyed_by_lowercase_hex_sha256(tmp_path):
    archive = _pack_one_clump(tmp_path)
    idx = build_index(archive, prefix=None, created_at=CREATED_AT)
    for key, entry in idx["files"].items():
        assert len(key) == 64
        assert key == key.lower()
        assert all(c in "0123456789abcdef" for c in key)


def test_deduped_entry_lists_both_paths_sorted(tmp_path):
    source_root = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    data = b"same content"
    f1 = _file_to_pack(source_root, "b/dup.jpg", data)
    f2 = _file_to_pack(source_root, "a/dup.jpg", data, sha256=f1.sha256)
    (archive,) = _pack([f1, f2], dest_dir)
    idx = build_index(archive, prefix=None, created_at=CREATED_AT)
    entry = idx["files"][f1.sha256]
    assert entry["paths"] == ["a/dup.jpg", "b/dup.jpg"]


def test_clump_and_single_entries_omit_part_keys(tmp_path):
    clump_archive = _pack_one_clump(tmp_path)
    single_archive = _pack_one_single(tmp_path)
    for archive in (clump_archive, single_archive):
        idx = build_index(archive, prefix=None, created_at=CREATED_AT)
        for entry in idx["files"].values():
            assert "part" not in entry
            assert "parts" not in entry


def test_object_key_uses_prefix():
    assert archive_object_key("abc", "server01/") == "server01/archives/abc.tar"


def test_object_key_without_prefix_has_no_leading_slash():
    assert archive_object_key("abc", None) == "archives/abc.tar"


def test_object_key_appends_missing_trailing_slash():
    assert archive_object_key("abc", "server01") == "server01/archives/abc.tar"


def test_index_object_key():
    assert index_object_key("abc", "server01/") == "server01/index/abc.json"


# --- Robustness --------------------------------------------------------------


def test_index_is_json_round_trippable(tmp_path):
    archive = _pack_one_clump(tmp_path)
    idx = build_index(archive, prefix="server01/", created_at=CREATED_AT)
    assert json.loads(json.dumps(idx)) == idx


def test_rfc3339_rejects_naive_datetime():
    with pytest.raises(ValueError):
        rfc3339(datetime(2026, 1, 2, 3, 4, 5))


def test_files_keyed_by_content_id_when_present(tmp_path):
    source_root = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    data = b"y" * 500
    sha256 = "b" * 64
    content_id = "c" * 64
    f = _file_to_pack(source_root, "docs/report.pdf", data, sha256=sha256, content_id=content_id)
    [archive] = _pack([f], dest_dir)

    seen: list[tuple[str, str]] = []

    def sha256_encryptor(cid, sha):
        seen.append((cid, sha))
        return "sealed-token"

    idx = build_index(archive, prefix=None, created_at=CREATED_AT, sha256_encryptor=sha256_encryptor)

    assert content_id in idx["files"]
    assert sha256 not in idx["files"]
    assert idx["key_scheme"] == "content_id_v1"
    assert idx["files"][content_id]["sha256_enc"] == "sealed-token"
    assert seen == [(content_id, sha256)]
    # The real sha256 must not appear anywhere in the serialized index.
    assert sha256 not in json.dumps(idx)


def test_key_check_written_alongside_key_scheme_when_content_id_keyed(tmp_path):
    source_root = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    f = _file_to_pack(source_root, "docs/report.pdf", b"y" * 500, sha256="b" * 64, content_id="c" * 64)
    [archive] = _pack([f], dest_dir)

    idx = build_index(
        archive,
        prefix=None,
        created_at=CREATED_AT,
        sha256_encryptor=lambda cid, sha: "sealed-token",
        key_check="deadbeefcafef00d",
    )

    assert idx["key_scheme"] == "content_id_v1"
    assert idx["key_check"] == "deadbeefcafef00d"


def test_key_check_absent_from_legacy_sha256_keyed_index(tmp_path):
    source_root = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    f = _file_to_pack(source_root, "docs/report.pdf", b"y" * 500, sha256="b" * 64, content_id=None)
    [archive] = _pack([f], dest_dir)

    idx = build_index(archive, prefix=None, created_at=CREATED_AT, key_check="deadbeefcafef00d")

    assert "key_scheme" not in idx
    assert "key_check" not in idx


def test_content_id_keying_requires_sha256_encryptor(tmp_path):
    source_root = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    f = _file_to_pack(source_root, "docs/report.pdf", b"y" * 500, content_id="c" * 64)
    [archive] = _pack([f], dest_dir)

    with pytest.raises(ValueError):
        build_index(archive, prefix=None, created_at=CREATED_AT)


def test_files_keyed_by_sha256_when_no_content_id(tmp_path):
    # Legacy path: no master key available for this run, or an index built
    # before this feature existed - falls back to real sha256 keys and omits
    # key_scheme entirely (its absence is what marks an index as legacy).
    source_root = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    data = b"z" * 500
    sha256 = "d" * 64
    f = _file_to_pack(source_root, "docs/plain.pdf", data, sha256=sha256)
    [archive] = _pack([f], dest_dir)

    idx = build_index(archive, prefix=None, created_at=CREATED_AT)

    assert sha256 in idx["files"]
    assert "key_scheme" not in idx


def test_mixed_content_id_presence_within_archive_raises(tmp_path):
    source_root = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    f1 = _file_to_pack(source_root, "a.jpg", b"a" * 50, content_id="1" * 64)
    f2 = _file_to_pack(source_root, "b.jpg", b"b" * 50, content_id=None)
    [archive] = _pack([f1, f2], dest_dir)

    with pytest.raises(ValueError):
        build_index(archive, prefix=None, created_at=CREATED_AT)


def test_duplicate_hash_in_one_archive_raises():
    member1 = PackedMember(
        sha256="a" * 64,
        content_id=None,
        paths=["one.txt"],
        size_bytes=10,
        mtime_ns=1_700_000_000_000_000_000,
        member_name="one.txt",
        offset=512,
        part=None,
        part_count=None,
    )
    member2 = PackedMember(
        sha256="a" * 64,
        content_id=None,
        paths=["two.txt"],
        size_bytes=20,
        mtime_ns=1_700_000_000_000_000_000,
        member_name="two.txt",
        offset=1536,
        part=None,
        part_count=None,
    )
    archive = PackedArchive(
        archive_id="bad-archive",
        archive_type="clump",
        local_path=Path("/tmp/bad-archive.tar"),
        size_bytes=2048,
        members=[member1, member2],
    )
    with pytest.raises(ValueError):
        build_index(archive, prefix=None, created_at=CREATED_AT)
