"""
Thin wrapper around google-cloud-storage for uploading/downloading backup
files to/from a connected bucket. Mirrors web/app/gcs.py's client
construction; only the primitives copy_media_file/verify_copy_job need.
"""
import json
import time
from pathlib import Path
from typing import Callable

from google.cloud import storage
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/devstorage.read_write"]

# google-cloud-storage's default per-request timeout (60s) comfortably covers
# small objects, but archive chunk files can now be up to encryption.CHUNK_SIZE
# (256MB) - at this environment's measured upload speed (~35Mbps, i.e. ~60s to
# move 256MB on its own) that default timeout is right on the edge and trips
# under any real-world variance ("Connection aborted: The write operation
# timed out"). REQUEST_TIMEOUT_SECONDS gives real headroom; CHUNK_SIZE bounds
# each individual resumable-upload HTTP request so a slow/interrupted
# connection only has to retry one small piece, not the whole object.
REQUEST_TIMEOUT_SECONDS = 600
CHUNK_SIZE = 8 * 1024 * 1024

# Real uploads are deliberately capped at this fraction of the link's last
# measured capacity (CloudStorageConfig.upload_mbps) rather than using all of
# it - a backup saturating the connection would interfere with everything
# else on it. web/app/main.py's ETA estimate uses the same fraction so
# displayed estimates match what actually happens; keep the two in sync.
UPLOAD_THROTTLE_FRACTION = 0.5

# Conservative cap applied before any speed test has run, so a first backup
# on an unmeasured connection is still throttled rather than going flat out.
# web/app/main.py's ETA estimate uses the same fallback; keep them in sync.
DEFAULT_UPLOAD_CAP_MBPS = 15.0


class _ThrottledReader:
    """Wraps a binary file object so sequential reads average out to at most
    max_bytes_per_sec - passed to Blob.upload_from_file so the resumable
    uploader's own chunked reads get paced without changing what's actually
    sent."""

    def __init__(self, fileobj, max_bytes_per_sec: float):
        self._f = fileobj
        self._max_bps = max_bytes_per_sec
        self._start = time.monotonic()
        self._sent = 0

    def read(self, size=-1):
        chunk = self._f.read(size)
        if chunk:
            self._sent += len(chunk)
            expected_elapsed = self._sent / self._max_bps
            actual_elapsed = time.monotonic() - self._start
            if expected_elapsed > actual_elapsed:
                time.sleep(expected_elapsed - actual_elapsed)
        return chunk

    def __getattr__(self, name):
        return getattr(self._f, name)


def effective_upload_mbps(upload_mbps: float | None, max_upload_mbps: float | None = None) -> float:
    """The upload cap actually in effect, in Mbps - always a number, never
    unthrottled: `max_upload_mbps` (TransferConfig's user-set ceiling) if one
    is configured, else UPLOAD_THROTTLE_FRACTION of the last measured link
    capacity, else DEFAULT_UPLOAD_CAP_MBPS as a conservative fallback before
    any speed test has run. An explicit `max_upload_mbps` always wins - it is
    a hard ceiling the user chose, not intersected with the measured-speed
    default (so setting it lower always takes effect, and setting it above
    the auto default raises the cap, not just lowers it)."""
    if max_upload_mbps:
        return max_upload_mbps
    if upload_mbps:
        return upload_mbps * UPLOAD_THROTTLE_FRACTION
    return DEFAULT_UPLOAD_CAP_MBPS


def upload_mbps_to_throttle_bytes_per_sec(upload_mbps: float | None, max_upload_mbps: float | None = None) -> float:
    """effective_upload_mbps(...), converted to bytes/sec for _ThrottledReader."""
    return effective_upload_mbps(upload_mbps, max_upload_mbps) * 1_000_000 / 8


class _ProgressReader:
    """Wraps a binary file object and reports how far into it the uploader has
    read, via progress_cb(position). Position-based (tell()), not a running
    sum, so a resumable upload that seeks back to retry a chunk reports
    honestly instead of double-counting."""

    def __init__(self, fileobj, progress_cb: Callable[[int], None]):
        self._f = fileobj
        self._cb = progress_cb

    def read(self, size=-1):
        chunk = self._f.read(size)
        if chunk:
            self._cb(self._f.tell())
        return chunk

    def __getattr__(self, name):
        return getattr(self._f, name)


def _client(service_account_json: str, project_id: str | None = None) -> storage.Client:
    info = json.loads(service_account_json)
    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return storage.Client(project=project_id or info.get("project_id"), credentials=credentials)


def upload_file(
    service_account_json: str,
    bucket_name: str,
    object_name: str,
    local_path,
    project_id: str | None = None,
    max_bytes_per_sec: float | None = None,
    progress_cb: Callable[[int], None] | None = None,
    storage_class: str | None = None,
) -> None:
    """storage_class (e.g. "ARCHIVE") is the class the new object is created
    in; None leaves it to the bucket's default. progress_cb(bytes_read_so_far) is called as the uploader consumes the
    file - in CHUNK_SIZE steps, since the resumable uploader reads a chunk at
    a time - so callers can show real transfer progress."""
    client = _client(service_account_json, project_id)
    blob = client.bucket(bucket_name).blob(object_name)
    blob.chunk_size = CHUNK_SIZE
    if storage_class:
        blob.storage_class = storage_class
    if max_bytes_per_sec or progress_cb:
        size = Path(local_path).stat().st_size
        with open(local_path, "rb") as f:
            stream = _ThrottledReader(f, max_bytes_per_sec) if max_bytes_per_sec else f
            if progress_cb:
                stream = _ProgressReader(stream, progress_cb)
            blob.upload_from_file(stream, size=size, timeout=REQUEST_TIMEOUT_SECONDS)
    else:
        blob.upload_from_filename(str(local_path), timeout=REQUEST_TIMEOUT_SECONDS)


def download_file(service_account_json: str, bucket_name: str, object_name: str, local_path, project_id: str | None = None) -> None:
    client = _client(service_account_json, project_id)
    blob = client.bucket(bucket_name).blob(object_name)
    blob.chunk_size = CHUNK_SIZE
    blob.download_to_filename(str(local_path), timeout=REQUEST_TIMEOUT_SECONDS)


def blob_exists(service_account_json: str, bucket_name: str, object_name: str, project_id: str | None = None) -> bool:
    client = _client(service_account_json, project_id)
    return client.bucket(bucket_name).blob(object_name).exists()


