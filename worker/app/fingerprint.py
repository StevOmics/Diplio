import hashlib
from dataclasses import dataclass
from pathlib import Path

# Mirrors web/app/fingerprint.py - keep the algorithm identical so fingerprints
# computed here are comparable to the ones stored by the web service's scan.
SAMPLE_SIZE = 1024 * 1024


def compute_fingerprint(path: Path, size_bytes: int) -> str:
    hasher = hashlib.blake2b(digest_size=32)
    hasher.update(str(size_bytes).encode())

    with path.open("rb") as f:
        hasher.update(f.read(SAMPLE_SIZE))
        if size_bytes > SAMPLE_SIZE:
            f.seek(max(size_bytes - SAMPLE_SIZE, SAMPLE_SIZE))
            hasher.update(f.read())

    return hasher.hexdigest()


# Larger than SAMPLE_SIZE above on purpose: this reads the whole file rather
# than a head/tail sample, so fewer, bigger reads matter more than for the
# fingerprint - 4 MiB keeps a multi-GB movie's syscall count down while still
# being trivial memory to hold per chunk.
HASH_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True)
class HashedFile:
    sha256: str
    size_bytes: int
    mtime_ns: int


class FileChangedDuringRead(Exception):
    """A file's size or mtime moved while it was being hashed, so the digest
    cannot be trusted to describe any single version of it."""


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 of the whole file (spec section 1.2)."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(HASH_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_file(path: Path) -> HashedFile:
    """sha256_file plus the spec section 6.2 guard: stats before and after,
    and raises FileChangedDuringRead if size or mtime_ns moved."""
    before = path.stat()
    # Called as a module-level attribute (not inlined here) so callers/tests
    # can monkeypatch sha256_file to simulate a file changing mid-read.
    digest = sha256_file(path)
    after = path.stat()

    # Raise rather than return None: this is deliberate so the spec section
    # 6.2 skip-and-log guard can't be silently bypassed by a caller that
    # forgets to check a return value. There is no retry in v2.0.
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise FileChangedDuringRead(
            f"{path} changed while being hashed: "
            f"before (size={before.st_size}, mtime_ns={before.st_mtime_ns}), "
            f"after (size={after.st_size}, mtime_ns={after.st_mtime_ns})"
        )

    return HashedFile(sha256=digest, size_bytes=after.st_size, mtime_ns=after.st_mtime_ns)
