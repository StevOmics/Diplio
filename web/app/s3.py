"""
Thin wrapper around boto3 for the Settings page's AWS S3 connection workflow.
Mirrors web/app/gcs.py's shape (same function names/signatures where the two
providers' concepts line up) so the two Settings panels and their routes stay
symmetric. Credentials are an access key id/secret pair (S3StorageConfig in
the database), not a key file - nothing here ever touches disk for auth.
"""
import secrets
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

SPEED_TEST_PREFIX = "_mediabridge_speedtest/"
SPEED_TEST_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB, same as gcs.py's test

# Classes an archive can be created with; None (not listed) = the bucket's default.
STORAGE_CLASSES = ("STANDARD", "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING", "GLACIER", "GLACIER_IR", "DEEP_ARCHIVE")


def _client(access_key_id: str, secret_access_key: str, region: str | None = None):
    return boto3.client(
        "s3",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region or None,
    )


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Message") or str(exc)
    return str(exc)


def verify_credentials(access_key_id: str, secret_access_key: str, region: str | None = None) -> None:
    """Raises ValueError with a user-presentable message if these credentials
    don't work at all (list_buckets needs no bucket-level permission, so it
    doubles as a basic auth check)."""
    try:
        _client(access_key_id, secret_access_key, region).list_buckets()
    except (ClientError, BotoCoreError) as exc:
        raise ValueError(_friendly_error(exc)) from exc


def list_buckets(access_key_id: str, secret_access_key: str, region: str | None = None) -> list[str]:
    client = _client(access_key_id, secret_access_key, region)
    try:
        return sorted(b["Name"] for b in client.list_buckets().get("Buckets", []))
    except (ClientError, BotoCoreError) as exc:
        raise ValueError(_friendly_error(exc)) from exc


def verify_bucket_access(access_key_id: str, secret_access_key: str, bucket_name: str, region: str | None = None) -> None:
    """Raises if the bucket doesn't exist or isn't reachable with these credentials."""
    client = _client(access_key_id, secret_access_key, region)
    try:
        client.head_bucket(Bucket=bucket_name)
    except (ClientError, BotoCoreError) as exc:
        raise ValueError(f"Bucket '{bucket_name}' doesn't exist or isn't accessible: {_friendly_error(exc)}") from exc


def _mbps(payload_bytes: int, seconds: float) -> float:
    return (payload_bytes * 8 / 1_000_000) / seconds if seconds > 0 else 0.0


def test_connectivity_and_speed(
    access_key_id: str, secret_access_key: str, bucket_name: str, region: str | None = None
) -> dict:
    """Uploads then downloads a throwaway random-content object to measure
    real upload/download throughput, deleting it afterward either way.
    Mirrors gcs.py's test_connectivity_and_speed."""
    client = _client(access_key_id, secret_access_key, region)
    try:
        client.head_bucket(Bucket=bucket_name)
    except (ClientError, BotoCoreError) as exc:
        raise ValueError(f"Bucket '{bucket_name}' doesn't exist or isn't accessible: {_friendly_error(exc)}") from exc

    payload = secrets.token_bytes(SPEED_TEST_PAYLOAD_BYTES)
    key = f"{SPEED_TEST_PREFIX}{secrets.token_hex(8)}.bin"
    try:
        start = time.monotonic()
        client.put_object(Bucket=bucket_name, Key=key, Body=payload, ContentType="application/octet-stream")
        upload_seconds = time.monotonic() - start

        start = time.monotonic()
        downloaded = client.get_object(Bucket=bucket_name, Key=key)["Body"].read()
        download_seconds = time.monotonic() - start
        if downloaded != payload:
            raise ValueError("Downloaded test data didn't match what was uploaded")
    except (ClientError, BotoCoreError) as exc:
        raise ValueError(_friendly_error(exc)) from exc
    finally:
        try:
            client.delete_object(Bucket=bucket_name, Key=key)
        except Exception:
            pass  # best-effort cleanup - a stray test object isn't worth failing the test over

    return {
        "payload_bytes": SPEED_TEST_PAYLOAD_BYTES,
        "upload_mbps": _mbps(SPEED_TEST_PAYLOAD_BYTES, upload_seconds),
        "download_mbps": _mbps(SPEED_TEST_PAYLOAD_BYTES, download_seconds),
    }


def list_prefixes(
    access_key_id: str, secret_access_key: str, bucket_name: str, prefix: str = "", region: str | None = None
) -> list[str]:
    """Immediate "subfolders" under a bucket prefix (S3 has no real folders:
    these are the CommonPrefixes at the "/" delimiter). Returns bare folder
    names, sorted. `prefix` must be "" or end with "/"."""
    client = _client(access_key_id, secret_access_key, region)
    paginator = client.get_paginator("list_objects_v2")
    names: set[str] = set()
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix or "", Delimiter="/"):
        for common in page.get("CommonPrefixes", []):
            names.add(common["Prefix"][len(prefix):].rstrip("/"))
    return sorted(names)


def create_folder(
    access_key_id: str, secret_access_key: str, bucket_name: str, prefix: str, region: str | None = None
) -> None:
    """Makes a "folder" exist by writing the conventional zero-byte placeholder
    object named "<prefix>/" - S3 has no real directories; without this a
    folder only appears once something is uploaded under it. No-op if a
    placeholder already exists (mirrors gcs.py's create_folder)."""
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    client = _client(access_key_id, secret_access_key, region)
    try:
        client.head_object(Bucket=bucket_name, Key=prefix)
        return  # already exists
    except ClientError:
        pass
    client.put_object(Bucket=bucket_name, Key=prefix, Body=b"", ContentType="application/x-directory")


def list_objects(
    access_key_id: str, secret_access_key: str, bucket_name: str, prefix: str, region: str | None = None
) -> list[tuple[str, int, str | None]]:
    """(key, size, storage_class) of every object under prefix, recursively."""
    client = _client(access_key_id, secret_access_key, region)
    paginator = client.get_paginator("list_objects_v2")
    results: list[tuple[str, int, str | None]] = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix or ""):
        for obj in page.get("Contents", []):
            results.append((obj["Key"], obj["Size"], obj.get("StorageClass")))
    return results


def read_object(
    access_key_id: str, secret_access_key: str, bucket_name: str, key: str, region: str | None = None
) -> bytes | None:
    """Downloads one small object (an index.json - never a tar payload), or
    None if it no longer exists. Mirrors gcs.py's read_object."""
    client = _client(access_key_id, secret_access_key, region)
    try:
        return client.get_object(Bucket=bucket_name, Key=key)["Body"].read()
    except Exception:
        return None
