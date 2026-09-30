"""StorageBackend protocol and verified upload for the v2 backup pipeline
(docs/cfa-spec.md sections 4 and 6.4). No imports from app.models or app.db -
this module is pure storage plumbing; step 6 wires it to the database.

Nothing in the rest of the app calls into this module yet - step 6 is the
first consumer. See docs/backup-plan/steps/05-storage-and-upload.md.
"""
from __future__ import annotations

import base64
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import boto3
import google_crc32c
from botocore.exceptions import ClientError
from google.api_core.exceptions import NotFound

from app import gcs
from app.archive_paths import is_gcs_path, is_s3_path, parse_gcs_path, parse_s3_path

# Matches fingerprint.HASH_CHUNK_SIZE's reasoning: never read a multi-GB
# archive into memory at once.
CRC32C_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True)
class ObjectStat:
    size: int
    crc32c: str  # base64, exactly as GCS reports it


class UploadVerificationError(Exception):
    """Raised when an uploaded object's confirmed size/CRC32C never match the
    local file's, after all retry attempts are exhausted. A mismatch is never
    treated as success - see upload_and_confirm."""


class StorageConfigError(RuntimeError):
    """Raised by backend_for_destination when the provider required by an
    archive's path (s3:// or gs:///gcs://) isn't configured. A RuntimeError
    subclass so existing `except RuntimeError` callers keep working;
    backup_run.py wraps this into its own BackupRunRefused."""


def crc32c_base64(data: bytes) -> str:
    checksum = google_crc32c.Checksum()
    checksum.update(data)
    return base64.b64encode(checksum.digest()).decode("ascii")


def crc32c_base64_file(path: Path) -> str:
    checksum = google_crc32c.Checksum()
    with open(path, "rb") as f:
        while chunk := f.read(CRC32C_CHUNK_SIZE):
            checksum.update(chunk)
    return base64.b64encode(checksum.digest()).decode("ascii")


class StorageBackend(Protocol):
    """A typing.Protocol, not an ABC, so the test double (tests/storage_double.py)
    stays independent of production code - it implements this shape without
    inheriting from anything here."""

    def upload(
        self, key: str, local_path: Path, *, max_bytes_per_sec: float | None = None, progress_cb=None
    ) -> None: ...

    def write_bytes(self, key: str, data: bytes, *, content_type: str = "application/json") -> None: ...

    def download(self, key: str, local_path: Path) -> None: ...

    def read_range(self, key: str, offset: int, length: int) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def stat(self, key: str) -> ObjectStat: ...

    # The three below exist to keep verify cheap (see tasks._VerifyContext):
    # one request each, on the backend's own client, with "missing" reported
    # as a value rather than an exception (and a second request to find out).
    def stat_or_none(self, key: str) -> ObjectStat | None: ...

    def read_object(self, key: str) -> bytes | None: ...

    def list_stats(self, prefix: str) -> dict[str, ObjectStat]: ...


class GCSBackend:
    """Implements StorageBackend over app.gcs's client construction and the
    google-cloud-storage client directly for the operations app.gcs has no
    primitive for (write_bytes, read_range, stat, delete). Wraps app.gcs's
    existing functions rather than reimplementing them - see "Do Not Touch"
    in the step file."""

    def __init__(
        self,
        service_account_json: str,
        bucket_name: str,
        project_id: str | None = None,
        storage_class: str | None = None,
    ):
        # Class for objects this backend creates (upload, write_bytes); None
        # = the bucket's default.
        self._storage_class = storage_class
        self._service_account_json = service_account_json
        self._bucket_name = bucket_name
        self._project_id = project_id
        self._client = gcs._client(service_account_json, project_id)
        self._bucket = self._client.bucket(bucket_name)

    def upload(
        self, key: str, local_path: Path, *, max_bytes_per_sec: float | None = None, progress_cb=None
    ) -> None:
        gcs.upload_file(
            self._service_account_json,
            self._bucket_name,
            key,
            local_path,
            project_id=self._project_id,
            max_bytes_per_sec=max_bytes_per_sec,
            progress_cb=progress_cb,
            storage_class=self._storage_class,
        )

    def write_bytes(self, key: str, data: bytes, *, content_type: str = "application/json") -> None:
        blob = self._bucket.blob(key)
        if self._storage_class:
            blob.storage_class = self._storage_class
        blob.upload_from_string(data, content_type=content_type, timeout=gcs.REQUEST_TIMEOUT_SECONDS)

    def download(self, key: str, local_path: Path) -> None:
        gcs.download_file(
            self._service_account_json,
            self._bucket_name,
            key,
            local_path,
            project_id=self._project_id,
        )

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        # GCS's `end` is inclusive - off by one here surfaces much later as a
        # one-byte-short restore (docs/cfa-spec.md section 7).
        if length == 0:
            return b""
        blob = self._bucket.blob(key)
        return blob.download_as_bytes(start=offset, end=offset + length - 1)

    def exists(self, key: str) -> bool:
        return gcs.blob_exists(self._service_account_json, self._bucket_name, key, project_id=self._project_id)

    def delete(self, key: str) -> None:
        # Idempotent: a missing object is treated as already-deleted so step
        # 6's cleanup and any later retry are safe to repeat.
        blob = self._bucket.blob(key)
        try:
            blob.delete()
        except NotFound:
            pass

    def stat(self, key: str) -> ObjectStat:
        # reload() so size/crc32c come from the service, not a stale local
        # Blob object that was never fetched.
        blob = self._bucket.blob(key)
        blob.reload()
        return ObjectStat(size=blob.size, crc32c=blob.crc32c)

    def stat_or_none(self, key: str) -> ObjectStat | None:
        # One metadata request; get_blob returns None on a 404.
        blob = self._bucket.get_blob(key)
        return None if blob is None else ObjectStat(size=blob.size, crc32c=blob.crc32c)

    def read_object(self, key: str) -> bytes | None:
        # One download request; None if the object is missing.
        try:
            return self._bucket.blob(key).download_as_bytes(timeout=gcs.REQUEST_TIMEOUT_SECONDS)
        except NotFound:
            return None

    def list_stats(self, prefix: str) -> dict[str, ObjectStat]:
        # One request per 1000 objects - vastly cheaper than a stat each when
        # many objects under the prefix are wanted. Metadata only: no data is
        # read, so no retrieval fee on Nearline/Coldline/Archive buckets.
        return {
            blob.name: ObjectStat(size=blob.size, crc32c=blob.crc32c)
            for blob in self._client.list_blobs(self._bucket_name, prefix=prefix)
        }


class S3Backend:
    """Implements StorageBackend over boto3's S3 client. Every write requests
    a full-object CRC32C checksum (ChecksumAlgorithm="CRC32C" / ChecksumMode
    "ENABLED" on reads) so ObjectStat.crc32c means the same thing as it does
    for GCSBackend, keeping upload_and_confirm and verify provider-agnostic -
    S3's ETag is an MD5 only for single-part uploads (multipart ETags aren't
    a valid hash at all), so it can't stand in for a real checksum here."""

    def __init__(
        self,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
        region: str | None = None,
        storage_class: str | None = None,
    ):
        self._storage_class = storage_class
        self._bucket_name = bucket_name
        self._client = boto3.client(
            "s3", aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key, region_name=region or None
        )

    def _extra_args(self) -> dict:
        args = {"ChecksumAlgorithm": "CRC32C"}
        if self._storage_class:
            args["StorageClass"] = self._storage_class
        return args

    def upload(
        self, key: str, local_path: Path, *, max_bytes_per_sec: float | None = None, progress_cb=None
    ) -> None:
        # boto3's upload_fileobj streams via its TransferManager (multipart for
        # large files) rather than reading the whole archive into memory. When
        # throttled or progress is wanted, the file is opened directly and
        # wrapped in the same gcs._ThrottledReader used by GCSBackend, so both
        # backends honor max_bytes_per_sec identically.
        callback = (lambda sent: progress_cb(sent)) if progress_cb else None
        if max_bytes_per_sec:
            with open(local_path, "rb") as f:
                stream = gcs._ThrottledReader(f, max_bytes_per_sec)
                self._client.upload_fileobj(
                    stream, self._bucket_name, key, ExtraArgs=self._extra_args(), Callback=callback
                )
        else:
            self._client.upload_file(
                str(local_path), self._bucket_name, key, ExtraArgs=self._extra_args(), Callback=callback
            )

    def write_bytes(self, key: str, data: bytes, *, content_type: str = "application/json") -> None:
        self._client.put_object(Bucket=self._bucket_name, Key=key, Body=data, ContentType=content_type, **self._extra_args())

    def download(self, key: str, local_path: Path) -> None:
        self._client.download_file(self._bucket_name, key, str(local_path))

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        if length == 0:
            return b""
        resp = self._client.get_object(Bucket=self._bucket_name, Key=key, Range=f"bytes={offset}-{offset + length - 1}")
        return resp["Body"].read()

    def exists(self, key: str) -> bool:
        return self.stat_or_none(key) is not None

    def delete(self, key: str) -> None:
        # S3's delete_object is already idempotent - no error on a missing key.
        self._client.delete_object(Bucket=self._bucket_name, Key=key)

    def _stat_from_head(self, head: dict) -> ObjectStat:
        # ChecksumCRC32C is base64, like GCS's - present only when the object
        # was written with ChecksumAlgorithm="CRC32C" (everything this app
        # writes is; an object from outside this app might not have one).
        return ObjectStat(size=head["ContentLength"], crc32c=head.get("ChecksumCRC32C", ""))

    def stat(self, key: str) -> ObjectStat:
        return self._stat_from_head(self._client.head_object(Bucket=self._bucket_name, Key=key, ChecksumMode="ENABLED"))

    def stat_or_none(self, key: str) -> ObjectStat | None:
        try:
            return self._stat_from_head(
                self._client.head_object(Bucket=self._bucket_name, Key=key, ChecksumMode="ENABLED")
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise

    def read_object(self, key: str) -> bytes | None:
        try:
            return self._client.get_object(Bucket=self._bucket_name, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise

    def list_stats(self, prefix: str) -> dict[str, ObjectStat]:
        # Unlike GCSBackend, S3's ListObjectsV2 doesn't return checksums (only
        # ETag, which isn't a usable hash for multipart uploads) - this falls
        # back to one HeadObject per key found, so it's correct but not the
        # same "one request per 1000 objects" cost win verify gets on GCS.
        # Acceptable for now; a real follow-up if S3 verify volume gets large.
        paginator = self._client.get_paginator("list_objects_v2")
        stats: dict[str, ObjectStat] = {}
        for page in paginator.paginate(Bucket=self._bucket_name, Prefix=prefix):
            for obj in page.get("Contents", []):
                found = self.stat_or_none(obj["Key"])
                if found is not None:
                    stats[obj["Key"]] = found
        return stats


def backend_for_destination(db, destination, storage_class: str | None = None) -> StorageBackend:
    """Builds the right StorageBackend for this archive's path scheme
    (s3:// or gs:///gcs://), raising StorageConfigError if that provider isn't
    configured. The single dispatch point - backup_run.py, restore_run.py,
    tasks.py's verify, and sync_run.py all call this instead of hand-rolling
    their own GCS/S3 construction. Deferred imports of app.models avoid a
    hard dependency on the ORM for the parts of this module that don't need
    it (the storage_double.py test fixture, upload_and_confirm, etc.)."""
    from app.models import CloudStorageConfig, S3StorageConfig

    if is_s3_path(destination.path):
        s3_config = db.query(S3StorageConfig).first()
        if not s3_config or not s3_config.access_key_id or not s3_config.secret_access_key:
            raise StorageConfigError("no AWS S3 credentials are configured")
        bucket, _ = parse_s3_path(destination.path)
        return S3Backend(
            s3_config.access_key_id, s3_config.secret_access_key, bucket, s3_config.region, storage_class=storage_class
        )

    cloud_config = db.query(CloudStorageConfig).first()
    if not cloud_config or not cloud_config.service_account_json:
        raise StorageConfigError("no Google Cloud service account is configured")
    if is_gcs_path(destination.path):
        bucket = parse_gcs_path(destination.path)[0]
    elif cloud_config.bucket_name:
        # Legacy: an archive created before gs:// paths existed, falling
        # back to the single bucket in the cloud config.
        bucket = cloud_config.bucket_name
    else:
        raise StorageConfigError(f"archive {destination.name!r} has no gs://bucket path")
    return GCSBackend(cloud_config.service_account_json, bucket, cloud_config.project_id, storage_class=storage_class)


def upload_and_confirm(
    backend: StorageBackend,
    key: str,
    local_path: Path,
    *,
    attempts: int = 5,
    sleep=time.sleep,
    max_bytes_per_sec: float | None = None,
    progress_cb=None,
    on_checksummed=None,
) -> ObjectStat:
    """Uploads local_path to key, then confirms the stored object's size and
    CRC32C match before returning (docs/cfa-spec.md section 6.4). A mismatch -
    or an exception from the backend - triggers a retry with exponential
    backoff plus jitter, up to `attempts` times total. Never returns on a
    mismatch; raises UploadVerificationError once attempts are exhausted.

    The local file's size and checksum are computed exactly once, before the
    retry loop, so a retrying upload of a multi-GB archive doesn't re-read it
    on every attempt.

    progress_cb(bytes_sent) is forwarded to the backend for each attempt
    (it restarts from 0 on a retry); on_checksummed() is called once the
    up-front checksum is done, just before the first attempt, so callers can
    tell the (silent) checksum pause from the transfer itself.
    """
    local_path = Path(local_path)
    expected_size = local_path.stat().st_size
    expected_crc32c = crc32c_base64_file(local_path)
    if on_checksummed is not None:
        on_checksummed()
    upload_kwargs = {"progress_cb": progress_cb} if progress_cb is not None else {}

    found: ObjectStat | None = None
    error: Exception | None = None

    for attempt in range(1, attempts + 1):
        found = None
        error = None
        try:
            backend.upload(key, local_path, max_bytes_per_sec=max_bytes_per_sec, **upload_kwargs)
            found = backend.stat(key)
            if found.size == expected_size and found.crc32c == expected_crc32c:
                return found
        except Exception as exc:  # noqa: BLE001 - any backend failure is a retry candidate
            error = exc

        if attempt < attempts:
            sleep(2 ** (attempt - 1) + random.uniform(0, 1))

    if found is not None:
        detail = f"found size={found.size} crc32c={found.crc32c}"
    else:
        detail = f"last error: {error!r}"
    raise UploadVerificationError(
        f"upload verification failed for {key!r} after {attempts} attempts: "
        f"expected size={expected_size} crc32c={expected_crc32c}; {detail}"
    )


class CountingBackend:
    """Wraps a backend and counts calls and bytes moved - each call is one
    billable request against a real bucket. Used to log what a verify batch
    cost, and by tests to assert on it. A list_stats call is counted as one
    request per 1000 objects returned (what GCS actually charges)."""

    def __init__(self, inner):
        self._inner = inner
        self.ops: dict[str, int] = {}
        self.bytes_up = 0
        self.bytes_down = 0

    def _count(self, name: str, n: int = 1) -> None:
        self.ops[name] = self.ops.get(name, 0) + n

    @property
    def total_ops(self) -> int:
        return sum(self.ops.values())

    def reset(self) -> None:
        self.ops.clear()
        self.bytes_up = self.bytes_down = 0

    def summary(self) -> str:
        ops = ", ".join(f"{k}={v}" for k, v in sorted(self.ops.items())) or "none"
        return f"{self.total_ops} requests ({ops}), {self.bytes_down} bytes read, {self.bytes_up} bytes written"

    def upload(self, key, local_path, *, max_bytes_per_sec=None, progress_cb=None):
        self._count("upload")
        self.bytes_up += Path(local_path).stat().st_size
        kwargs = {"progress_cb": progress_cb} if progress_cb is not None else {}
        self._inner.upload(key, local_path, max_bytes_per_sec=max_bytes_per_sec, **kwargs)

    def write_bytes(self, key, data, *, content_type="application/json"):
        self._count("write_bytes")
        self.bytes_up += len(data)
        self._inner.write_bytes(key, data, content_type=content_type)

    def download(self, key, local_path):
        self._count("download")
        self._inner.download(key, local_path)
        self.bytes_down += Path(local_path).stat().st_size

    def read_range(self, key, offset, length):
        self._count("read_range")
        self.bytes_down += length
        return self._inner.read_range(key, offset, length)

    def read_object(self, key):
        self._count("read_object")
        data = self._inner.read_object(key)
        self.bytes_down += len(data or b"")
        return data

    def exists(self, key):
        self._count("exists")
        return self._inner.exists(key)

    def stat(self, key):
        self._count("stat")
        return self._inner.stat(key)

    def stat_or_none(self, key):
        self._count("stat")
        return self._inner.stat_or_none(key)

    def list_stats(self, prefix):
        result = self._inner.list_stats(prefix)
        self._count("list", max(1, -(-len(result) // 1000)))
        return result

    def delete(self, key):
        self._count("delete")
        self._inner.delete(key)

    def __getattr__(self, name):
        return getattr(self._inner, name)
