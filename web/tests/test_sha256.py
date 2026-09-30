import hashlib
import os
from pathlib import Path

import pytest

import app.fingerprint as fingerprint
from app.fingerprint import FileChangedDuringRead, HASH_CHUNK_SIZE, hash_file, sha256_file

# Known-answer vectors, duplicated in worker/tests/test_sha256.py on purpose - see
# the comment on the fingerprint KNOWN_*_FINGERPRINT constants in
# test_fingerprint.py. web and worker each carry their own copy of this
# algorithm, so both copies must agree on the same inputs.
KNOWN_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
KNOWN_HELLO_WORLD_SHA256 = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
KNOWN_CHUNK_BOUNDARY_SHA256 = "299285fc41a44cdb038b9fdaf494c76ca9d0c866672b2b266c1a0c17dda60a05"


def test_sha256_empty_file(tmp_path: Path):
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    assert sha256_file(f) == KNOWN_EMPTY_SHA256


def test_sha256_known_vector(tmp_path: Path):
    f = tmp_path / "hello.bin"
    f.write_bytes(b"hello world")
    assert sha256_file(f) == KNOWN_HELLO_WORLD_SHA256


def test_sha256_multi_chunk_matches_hashlib(tmp_path: Path):
    # 10 MiB, well past HASH_CHUNK_SIZE (4 MiB) - proves the chunk loop
    # accumulates across several reads rather than only hashing the first one.
    data = os.urandom(10 * 1024 * 1024)
    f = tmp_path / "random.bin"
    f.write_bytes(data)
    assert sha256_file(f) == hashlib.sha256(data).hexdigest()


def test_sha256_exact_chunk_boundary(tmp_path: Path):
    f = tmp_path / "boundary.bin"
    f.write_bytes(b"a" * HASH_CHUNK_SIZE)
    assert sha256_file(f) == KNOWN_CHUNK_BOUNDARY_SHA256


def test_sha256_chunk_boundary_plus_one(tmp_path: Path):
    data = b"a" * (HASH_CHUNK_SIZE + 1)
    f = tmp_path / "boundary_plus_one.bin"
    f.write_bytes(data)
    assert sha256_file(f) == hashlib.sha256(data).hexdigest()


def test_hash_file_reports_size_and_mtime(tmp_path: Path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"some content")
    result = hash_file(f)
    st = f.stat()
    assert result.size_bytes == st.st_size
    assert result.mtime_ns == st.st_mtime_ns
    assert result.sha256 == sha256_file(f)


def test_hash_file_raises_when_file_grows(tmp_path: Path, monkeypatch):
    f = tmp_path / "grows.bin"
    f.write_bytes(b"hello")

    def fake_sha256_file(path: Path) -> str:
        with path.open("ab") as fh:
            fh.write(b"!")
        return "dummydigest"

    monkeypatch.setattr(fingerprint, "sha256_file", fake_sha256_file)
    with pytest.raises(FileChangedDuringRead):
        hash_file(f)


def test_hash_file_raises_when_only_mtime_changes(tmp_path: Path, monkeypatch):
    f = tmp_path / "retouched.bin"
    f.write_bytes(b"hello")
    original_mtime_ns = f.stat().st_mtime_ns

    def fake_sha256_file(path: Path) -> str:
        path.write_bytes(b"hello")  # identical length and content
        os.utime(path, ns=(original_mtime_ns + 1_000_000_000, original_mtime_ns + 1_000_000_000))
        return "dummydigest"

    monkeypatch.setattr(fingerprint, "sha256_file", fake_sha256_file)
    with pytest.raises(FileChangedDuringRead):
        hash_file(f)


def test_file_changed_error_names_the_path(tmp_path: Path, monkeypatch):
    f = tmp_path / "named.bin"
    f.write_bytes(b"hello")

    def fake_sha256_file(path: Path) -> str:
        with path.open("ab") as fh:
            fh.write(b"!")
        return "dummydigest"

    monkeypatch.setattr(fingerprint, "sha256_file", fake_sha256_file)
    with pytest.raises(FileChangedDuringRead) as exc_info:
        hash_file(f)
    assert f.name in str(exc_info.value)
