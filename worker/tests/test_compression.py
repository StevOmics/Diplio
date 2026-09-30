"""Pure-logic tests for app/compression.py - no database, no bucket."""
from __future__ import annotations

import gzip
import os
import zlib

import pytest

from app.compression import (
    compress_if_worthwhile,
    crc32_and_size,
    decompress_prefix,
    gunzip_file,
    gzip_file,
    gzip_trailer,
    is_gzip_header,
    looks_incompressible,
)

TEXT = b"the quick brown fox jumps over the lazy dog\n" * 5000


def test_gzip_round_trip_and_is_standard_gzip(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(TEXT)
    gz = tmp_path / "a.txt.gz"
    size = gzip_file(src, gz)
    assert size == gz.stat().st_size < len(TEXT)
    assert gzip.decompress(gz.read_bytes()) == TEXT  # plain stdlib/gunzip can read it
    out = tmp_path / "out.txt"
    assert gunzip_file(gz, out) == len(TEXT)
    assert out.read_bytes() == TEXT


def test_gzip_output_is_deterministic(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(TEXT)
    gzip_file(src, tmp_path / "1.gz")
    gzip_file(src, tmp_path / "2.gz")
    assert (tmp_path / "1.gz").read_bytes() == (tmp_path / "2.gz").read_bytes()


def test_empty_file_round_trips(tmp_path):
    src = tmp_path / "empty"
    src.write_bytes(b"")
    gzip_file(src, tmp_path / "e.gz")
    assert gunzip_file(tmp_path / "e.gz", tmp_path / "e.out") == 0


def test_truncated_or_corrupt_gzip_is_rejected(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(TEXT)
    gz = tmp_path / "a.gz"
    gzip_file(src, gz)
    data = gz.read_bytes()
    (tmp_path / "trunc.gz").write_bytes(data[:-20])
    with pytest.raises((OSError, EOFError, zlib.error)):
        gunzip_file(tmp_path / "trunc.gz", tmp_path / "o1")
    flipped = bytearray(data)
    flipped[len(flipped) // 2] ^= 0xFF
    (tmp_path / "flip.gz").write_bytes(bytes(flipped))
    with pytest.raises((OSError, EOFError, zlib.error)):
        gunzip_file(tmp_path / "flip.gz", tmp_path / "o2")


@pytest.mark.parametrize("name", ["movie.mp4", "photo.JPG", "sheet.xlsx", "backup.zip", "song.mp3"])
def test_known_compressed_formats_are_skipped_without_sampling(tmp_path, name):
    src = tmp_path / name
    src.write_bytes(TEXT)  # compressible content, but the extension wins
    assert looks_incompressible(src, len(TEXT))
    assert compress_if_worthwhile(src, tmp_path / "x.gz", len(TEXT)) is None
    assert not (tmp_path / "x.gz").exists()


def test_random_data_is_skipped_by_the_sample_test(tmp_path):
    src = tmp_path / "blob.bin"
    src.write_bytes(os.urandom(300_000))
    assert looks_incompressible(src, 300_000)
    assert compress_if_worthwhile(src, tmp_path / "x.gz", 300_000) is None


def test_compressible_data_is_compressed(tmp_path):
    src = tmp_path / "notes.txt"
    src.write_bytes(TEXT)
    size = compress_if_worthwhile(src, tmp_path / "x.gz", len(TEXT))
    assert size is not None and size < len(TEXT) * 0.95
    assert (tmp_path / "x.gz").exists()


def test_tiny_file_that_would_grow_is_left_alone(tmp_path):
    src = tmp_path / "t.txt"
    src.write_bytes(b"hi")
    assert compress_if_worthwhile(src, tmp_path / "x.gz", 2) is None
    assert not (tmp_path / "x.gz").exists()


def test_trailer_header_and_prefix_helpers(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(TEXT)
    gz = tmp_path / "a.gz"
    gzip_file(src, gz)
    data = gz.read_bytes()
    assert is_gzip_header(data) and not is_gzip_header(TEXT)
    crc, isize = gzip_trailer(data)
    assert (crc, isize) == (zlib.crc32(TEXT) & 0xFFFFFFFF, len(TEXT))
    assert crc32_and_size(src) == (crc, len(TEXT))
    prefix = decompress_prefix(data[: len(data) // 2])  # a truncated stream is fine
    assert prefix and TEXT.startswith(prefix)
    with pytest.raises(ValueError):
        gzip_trailer(b"short")
