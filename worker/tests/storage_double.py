"""Test-only StorageBackend implementations (see app/storage.py's Protocol).

Deliberately lives under tests/, not app/, so it can never be wired into a
running deployment and so `LocalBackend` stays independent of production code
(app/storage.py's StorageBackend is a typing.Protocol, not an ABC - nothing
here inherits from anything in app/).

No test in this repo ever points a backend at a real bucket; everything here
operates on plain files under a tmp_path root.
"""
from __future__ import annotations

from pathlib import Path

from app.storage import ObjectStat, crc32c_base64


class LocalBackend:
    """Implements StorageBackend against files under `root`."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    def upload(self, key: str, local_path: Path, *, max_bytes_per_sec: float | None = None, progress_cb=None) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = Path(local_path).read_bytes()
        dest.write_bytes(data)
        if progress_cb:
            # Report in a few steps, like the real chunked uploader does.
            step = max(1, len(data) // 4)
            for sent in list(range(step, len(data), step)) + [len(data)]:
                progress_cb(sent)

    def write_bytes(self, key: str, data: bytes, *, content_type: str = "application/json") -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def download(self, key: str, local_path: Path) -> None:
        data = self._path(key).read_bytes()
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        data = self._path(key).read_bytes()
        if offset < 0 or length < 0 or offset + length > len(data):
            raise ValueError(
                f"read_range past end of object {key!r}: offset={offset} length={length} size={len(data)}"
            )
        return data[offset : offset + length]

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    def stat(self, key: str) -> ObjectStat:
        data = self._path(key).read_bytes()
        return ObjectStat(size=len(data), crc32c=crc32c_base64(data))

    def stat_or_none(self, key: str) -> ObjectStat | None:
        return self.stat(key) if self._path(key).exists() else None

    def read_object(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None

    def list_stats(self, prefix: str) -> dict[str, ObjectStat]:
        out = {}
        for path in self.root.rglob("*"):
            if path.is_file():
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    out[key] = self.stat(key)
        return out


class FlakyBackend:
    """Wraps another backend and raises `exc` on the 1-based `upload` call
    numbers listed in `fail_uploads_on`; everything else (including later,
    non-failing uploads) delegates straight through to `inner`."""

    def __init__(self, inner, *, fail_uploads_on: set[int], exc: Exception):
        self._inner = inner
        self._fail_uploads_on = fail_uploads_on
        self._exc = exc
        self.upload_calls = 0

    def upload(self, key: str, local_path: Path, *, max_bytes_per_sec: float | None = None, progress_cb=None) -> None:
        self.upload_calls += 1
        if self.upload_calls in self._fail_uploads_on:
            raise self._exc
        self._inner.upload(key, local_path, max_bytes_per_sec=max_bytes_per_sec, progress_cb=progress_cb)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class CorruptingBackend:
    """Wraps another backend and stores one flipped byte on every upload, so
    `upload_and_confirm` sees a genuine crc32c mismatch rather than a
    simulated failure."""

    def __init__(self, inner):
        self._inner = inner

    def upload(self, key: str, local_path: Path, *, max_bytes_per_sec: float | None = None, progress_cb=None) -> None:
        data = bytearray(Path(local_path).read_bytes())
        if data:
            data[0] ^= 0xFF
        else:
            data = bytearray(b"\x00")  # an empty file can't have a byte flipped
        self._inner.write_bytes(key, bytes(data))

    def __getattr__(self, name):
        return getattr(self._inner, name)


from app.storage import CountingBackend  # noqa: E402,F401 - re-exported for tests
