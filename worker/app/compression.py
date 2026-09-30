"""Optional gzip compression for the v2 backup pipeline
(docs/backup-plan/steps/14-compression.md).

Compression is applied to a file's bytes *before* encryption and packing:

    file -> gzip -> (AES-GCM blob) -> tar member -> clump/single/part archive

The file's SHA-256 is still taken over the original bytes, so dedupe,
skip-unchanged, key derivation and restore-time verification are unchanged.
Output is standard gzip (RFC 1952): `gunzip` reads it with no MediaBridge
software. It is deterministic (no embedded name or timestamp).

Only pure functions live here - no database, no bucket.
"""
from __future__ import annotations

import gzip
import shutil
import struct
import zlib
from pathlib import Path

COMPRESSION_GZIP = "gzip"
DEFAULT_LEVEL = 6

# A file is stored compressed only if that saves at least this fraction;
# otherwise the original bytes are stored (gzip adds ~18 bytes and, on
# incompressible data, a little more).
MIN_SAVING = 0.05

_COPY_CHUNK = 1024 * 1024
_SAMPLE_BYTES = 1024 * 1024

# Formats that are already compressed (or close enough that gzip will not earn
# its CPU). Checked first so a 50 GB movie is not even sampled. Anything not
# listed is still tested with a sample, so this only has to be a fast path.
ALREADY_COMPRESSED_EXTENSIONS = frozenset(
    {
        # video
        "mp4", "m4v", "mkv", "avi", "mov", "wmv", "flv", "webm", "mpg", "mpeg", "ts", "m2ts", "vob", "3gp",
        # images
        "jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "avif", "jp2",
        # audio
        "mp3", "m4a", "aac", "ogg", "opus", "flac", "wma",
        # archives / packaged documents (zip containers)
        "zip", "gz", "tgz", "bz2", "xz", "zst", "7z", "rar", "lz4",
        "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "jar", "apk",
    }
)


def _extension(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def gzip_file(source: Path, dest: Path, level: int = DEFAULT_LEVEL) -> int:
    """Streams source into a deterministic gzip file at dest; returns its size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, dest.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=level, mtime=0) as gz:
            shutil.copyfileobj(src, gz, _COPY_CHUNK)
    return dest.stat().st_size


def gunzip_file(source: Path, dest: Path) -> int:
    """Streams a gzip file out to dest; returns the decompressed size. Raises
    gzip.BadGzipFile / EOFError / zlib.error / OSError on a corrupt or
    truncated stream (gzip verifies its trailing CRC32 and length)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(source, "rb") as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out, _COPY_CHUNK)
    return dest.stat().st_size


def looks_incompressible(source: Path, size_bytes: int, level: int = DEFAULT_LEVEL) -> bool:
    """Cheap pre-check, so incompressible files are never fully compressed.
    True for known compressed formats, else if a sample of the file (the head
    and, for big files, a middle slice) does not shrink by MIN_SAVING."""
    if _extension(source) in ALREADY_COMPRESSED_EXTENSIONS:
        return True
    if size_bytes <= 0:
        return False
    with source.open("rb") as f:
        sample = f.read(_SAMPLE_BYTES)
        if size_bytes > 4 * _SAMPLE_BYTES:
            f.seek(size_bytes // 2)
            sample += f.read(_SAMPLE_BYTES)
    return len(zlib.compress(sample, level)) > len(sample) * (1 - MIN_SAVING)


def compress_if_worthwhile(source: Path, dest: Path, size_bytes: int, level: int = DEFAULT_LEVEL) -> int | None:
    """Gzips source to dest and returns the compressed size, or None (leaving
    nothing at dest) when the file should be stored as-is: a known compressed
    format, an incompressible sample, or a full result that saved too little."""
    if looks_incompressible(source, size_bytes, level):
        return None
    compressed_size = gzip_file(source, dest, level)
    if compressed_size > size_bytes * (1 - MIN_SAVING):
        dest.unlink(missing_ok=True)
        return None
    return compressed_size


# --- helpers for sampled verify ---------------------------------------------


def gzip_trailer(tail: bytes) -> tuple[int, int]:
    """(crc32, size mod 2**32) of the *uncompressed* data, from the last 8
    bytes of a gzip stream (RFC 1952 section 2.3)."""
    if len(tail) < 8:
        raise ValueError("gzip stream too short to hold a trailer")
    crc, isize = struct.unpack("<II", tail[-8:])
    return crc, isize


def is_gzip_header(head: bytes) -> bool:
    return head[:3] == b"\x1f\x8b\x08"


def decompress_prefix(head: bytes) -> bytes:
    """Decompresses as much of a gzip stream's start as `head` allows. A
    truncated stream is expected here, so no end-of-stream error is raised."""
    return zlib.decompressobj(wbits=31).decompress(head)


def crc32_and_size(path: Path) -> tuple[int, int]:
    crc = 0
    size = 0
    with path.open("rb") as f:
        while block := f.read(_COPY_CHUNK):
            crc = zlib.crc32(block, crc)
            size += len(block)
    return crc & 0xFFFFFFFF, size
