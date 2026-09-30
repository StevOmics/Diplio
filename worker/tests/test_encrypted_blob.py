"""Pure tests for the v2 encrypted-blob format (docs/backup-plan/steps/10-encryption.md):
blob round trip, tamper detection, boundary-aligned splitting, and the
encrypted index paths. No database or bucket."""

import hashlib
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from app.backup_index import build_index
from app.encryption import (
    BLOB_HEADER_SIZE,
    GCM_TAG_SIZE,
    blob_size_for,
    decrypt_blob,
    decrypt_paths,
    derive_file_key,
    encrypt_blob,
    encrypt_paths,
    encrypted_chunk_size,
)
from app.packer import MIB, FileToPack, pack, split_part_sizes

CHUNK = 4096  # tiny chunks so tests exercise many boundaries
KEY = derive_file_key(b"m" * 32, "a" * 64)


def _blob(tmp_path: Path, data: bytes, key: bytes = KEY, name: str = "f") -> Path:
    src = tmp_path / f"{name}.src"
    src.write_bytes(data)
    dst = tmp_path / f"{name}.blob"
    size = encrypt_blob(src, dst, key, chunk_size=CHUNK)
    assert size == dst.stat().st_size == blob_size_for(len(data), CHUNK)
    return dst


# --- round trip ---------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 3 * CHUNK + 7])
def test_round_trip_at_chunk_edges(tmp_path, size):
    data = os.urandom(size)
    blob = _blob(tmp_path, data)
    out = tmp_path / "out"
    assert decrypt_blob(blob, out, KEY) == size
    assert out.read_bytes() == data


def test_same_file_and_key_never_repeats_ciphertext(tmp_path):
    data = b"x" * 100
    a = _blob(tmp_path, data, name="a").read_bytes()
    b = _blob(tmp_path, data, name="b").read_bytes()
    assert a != b  # fresh nonce prefix per encryption
    assert a[4:8] != b[4:8]


def test_key_depends_on_file_hash():
    master = b"m" * 32
    assert derive_file_key(master, "a" * 64) != derive_file_key(master, "b" * 64)


# --- tamper detection ---------------------------------------------------------


def test_wrong_key_fails(tmp_path):
    blob = _blob(tmp_path, b"secret" * 1000)
    with pytest.raises(InvalidTag):
        decrypt_blob(blob, tmp_path / "out", derive_file_key(b"z" * 32, "a" * 64))
    assert not (tmp_path / "out").exists()


def test_flipped_byte_fails(tmp_path):
    blob = _blob(tmp_path, b"secret" * 1000)
    raw = bytearray(blob.read_bytes())
    raw[BLOB_HEADER_SIZE + 10] ^= 1
    blob.write_bytes(bytes(raw))
    with pytest.raises(InvalidTag):
        decrypt_blob(blob, tmp_path / "out", KEY)


def test_tampered_header_fails(tmp_path):
    blob = _blob(tmp_path, os.urandom(3 * CHUNK))
    raw = bytearray(blob.read_bytes())
    raw[19] ^= 1  # plain_size, authenticated as AAD
    blob.write_bytes(bytes(raw))
    with pytest.raises((InvalidTag, ValueError)):
        decrypt_blob(blob, tmp_path / "out", KEY)


def test_truncated_blob_fails(tmp_path):
    blob = _blob(tmp_path, os.urandom(3 * CHUNK))
    blob.write_bytes(blob.read_bytes()[: -(CHUNK + GCM_TAG_SIZE)])  # drop whole last chunk
    with pytest.raises(ValueError):
        decrypt_blob(blob, tmp_path / "out", KEY)


def test_reordered_chunks_fail(tmp_path):
    blob = _blob(tmp_path, os.urandom(3 * CHUNK))
    raw = blob.read_bytes()
    unit = encrypted_chunk_size(CHUNK)
    body = raw[BLOB_HEADER_SIZE:]
    swapped = raw[:BLOB_HEADER_SIZE] + body[unit : 2 * unit] + body[:unit] + body[2 * unit :]
    blob.write_bytes(swapped)
    with pytest.raises(InvalidTag):
        decrypt_blob(blob, tmp_path / "out", KEY)


def test_trailing_bytes_fail(tmp_path):
    blob = _blob(tmp_path, b"abc")
    blob.write_bytes(blob.read_bytes() + b"x")
    with pytest.raises(ValueError):
        decrypt_blob(blob, tmp_path / "out", KEY)


# --- boundary-aligned splitting -------------------------------------------------

MAX_SIZE = 2 * MIB  # payload ceiling 1 MiB
UNIT = encrypted_chunk_size(CHUNK)


@pytest.mark.parametrize("chunks", [1, 2, 255, 256, 257, 600])
def test_aligned_parts_cut_on_chunk_boundaries(chunks):
    size = BLOB_HEADER_SIZE + chunks * UNIT
    sizes = split_part_sizes(size, max_size=MAX_SIZE, align_unit=UNIT, align_head=BLOB_HEADER_SIZE)
    assert sum(sizes) == size
    assert all(s <= MIB for s in sizes)
    cut = 0
    for s in sizes[:-1]:
        cut += s
        assert (cut - BLOB_HEADER_SIZE) % UNIT == 0


def test_aligned_parts_with_short_last_chunk():
    size = blob_size_for(10 * CHUNK + 17, CHUNK)
    sizes = split_part_sizes(size, max_size=MAX_SIZE, align_unit=UNIT, align_head=BLOB_HEADER_SIZE)
    assert sum(sizes) == size


def test_aligned_split_rejects_unit_larger_than_ceiling():
    with pytest.raises(ValueError):
        split_part_sizes(10 * MIB, max_size=2 * MIB, align_unit=2 * MIB, align_head=BLOB_HEADER_SIZE)


def _pack_encrypted(tmp_path: Path, data: bytes, rel_path: str = "movies/secret name.mkv"):
    sha = hashlib.sha256(data).hexdigest()
    key = derive_file_key(b"m" * 32, sha)
    src = tmp_path / "plain"
    src.write_bytes(data)
    blob = tmp_path / f"{sha}.file"
    size = encrypt_blob(src, blob, key, chunk_size=CHUNK)
    out = tmp_path / "tars"
    out.mkdir()
    archives = pack(
        [FileToPack(blob, rel_path, sha, size, 1_700_000_000_000_000_000, member_stem=f"{sha}.file")],
        dest_dir=out,
        min_size=10,
        clump_size=MIB,
        max_size=MAX_SIZE,
        align_unit=UNIT,
        align_head=BLOB_HEADER_SIZE,
    )
    return sha, key, archives


@pytest.mark.parametrize("size", [50_000, 3 * MIB + 123, 4 * MIB])
def test_split_encrypted_file_reassembles_and_decrypts(tmp_path, size):
    data = os.urandom(size)
    sha, key, archives = _pack_encrypted(tmp_path, data)
    assert all(a.size_bytes <= MAX_SIZE for a in archives)

    parts = sorted(archives, key=lambda a: a.members[0].part or 0)
    joined = b""
    for a in parts:
        (m,) = a.members
        assert m.member_name.startswith(f"{sha}.file")
        assert "secret" not in m.member_name
        with tarfile.open(a.local_path) as tf:
            joined += tf.extractfile(tf.getmembers()[0]).read()

    combined = tmp_path / "combined.blob"
    combined.write_bytes(joined)
    out = tmp_path / "restored"
    decrypt_blob(combined, out, key)
    assert out.read_bytes() == data


def test_swapped_parts_are_detected(tmp_path):
    data = os.urandom(3 * MIB)
    sha, key, archives = _pack_encrypted(tmp_path, data)
    assert len(archives) >= 3
    bodies = []
    for a in sorted(archives, key=lambda a: a.members[0].part):
        with tarfile.open(a.local_path) as tf:
            bodies.append(tf.extractfile(tf.getmembers()[0]).read())
    bodies[1], bodies[2] = bodies[2], bodies[1]
    combined = tmp_path / "combined.blob"
    combined.write_bytes(b"".join(bodies))
    with pytest.raises((InvalidTag, ValueError)):
        decrypt_blob(combined, tmp_path / "restored", key)


# --- index ---------------------------------------------------------------------


def test_encrypted_index_hides_paths_but_keeps_hashes(tmp_path):
    data = os.urandom(5000)
    sha, key, archives = _pack_encrypted(tmp_path, data, rel_path="private/holiday photos/img.jpg")
    index = build_index(
        archives[0],
        prefix="p/",
        created_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
        path_encryptor=lambda h, paths: encrypt_paths(derive_file_key(b"m" * 32, h), h, paths),
    )
    text = str(index)
    assert "holiday" not in text and "private" not in text
    assert index["encrypted"] is True
    entry = index["files"][sha]
    assert "paths" not in entry
    assert entry["member"] == f"{sha}.file"
    assert decrypt_paths(key, sha, entry["paths_enc"]) == ["private/holiday photos/img.jpg"]


def test_paths_token_is_bound_to_its_hash():
    key = derive_file_key(b"m" * 32, "a" * 64)
    token = encrypt_paths(key, "a" * 64, ["x/y.txt"])
    with pytest.raises(InvalidTag):
        decrypt_paths(key, "b" * 64, token)


def test_plain_index_unchanged(tmp_path):
    src = tmp_path / "f"
    src.write_bytes(b"hello world")
    out = tmp_path / "t"
    out.mkdir()
    archives = pack(
        [FileToPack(src, "a/b.txt", hashlib.sha256(b"hello world").hexdigest(), 11, 1_700_000_000_000_000_000)],
        dest_dir=out,
        min_size=100,
        clump_size=MIB,
        max_size=MAX_SIZE,
    )
    index = build_index(archives[0], prefix=None, created_at=datetime(2026, 9, 19, tzinfo=timezone.utc))
    (entry,) = index["files"].values()
    assert entry["paths"] == ["a/b.txt"] and "encrypted" not in index
