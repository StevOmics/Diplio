"""Cloud archive paths: "gs://bucket/folder/sub" <-> (bucket, key prefix).

Pure string logic, no imports from app.models/app.db, so it stays in the
dependency-free test suite. web/app/archive_paths.py is a hand-mirrored copy
(the two services don't share a package - see CLAUDE.md).
"""
import re

_SCHEMES = ("gs://", "gcs://")
_S3_SCHEMES = ("s3://",)
# GCS bucket names: 3-63 chars of lowercase letters, digits, "-", "_", ".";
# starting and ending with a letter or digit.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
# S3 bucket names: 3-63 chars of lowercase letters, digits, "-", "."; starting
# and ending with a letter or digit. (Not enforcing the fuller AWS ruleset -
# no dot-adjacent-hyphen, no IP-address-shaped names - real violations get
# caught by S3 itself when the bucket is actually used.)
_S3_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def is_gcs_path(path: str | None) -> bool:
    return bool(path) and path.strip().lower().startswith(_SCHEMES)


def is_s3_path(path: str | None) -> bool:
    return bool(path) and path.strip().lower().startswith(_S3_SCHEMES)


def normalize_prefix(*parts: str | None) -> str:
    """Joins folder parts into a bucket-relative prefix: no leading slash,
    exactly one trailing slash, "" for the bucket root. Rejects ".." so a
    subfolder can never climb out of its archive."""
    segments: list[str] = []
    for part in parts:
        for seg in (part or "").replace("\\", "/").split("/"):
            seg = seg.strip()
            if not seg or seg == ".":
                continue
            if seg == "..":
                raise ValueError("folder path may not contain '..'")
            segments.append(seg)
    return "/".join(segments) + "/" if segments else ""


def parse_gcs_path(path: str) -> tuple[str, str]:
    """"gs://bucket/folder1/sub" -> ("bucket", "folder1/sub/"). Accepts the
    legacy "gcs://" scheme too. Raises ValueError with a user-presentable
    message on anything that isn't a valid bucket path."""
    raw = (path or "").strip()
    lowered = raw.lower()
    for scheme in _SCHEMES:
        if lowered.startswith(scheme):
            rest = raw[len(scheme):]
            break
    else:
        raise ValueError("path must start with gs://")
    bucket, _, folder = rest.partition("/")
    if not _BUCKET_RE.match(bucket):
        raise ValueError(f"'{bucket}' is not a valid bucket name")
    return bucket, normalize_prefix(folder)


def format_gcs_path(bucket: str, prefix: str = "") -> str:
    """Canonical display/storage form: gs://bucket or gs://bucket/folder (no trailing slash)."""
    prefix = normalize_prefix(prefix).rstrip("/")
    return f"gs://{bucket}/{prefix}" if prefix else f"gs://{bucket}"


def parse_s3_path(path: str) -> tuple[str, str]:
    """"s3://bucket/folder1/sub" -> ("bucket", "folder1/sub/"). Mirrors
    parse_gcs_path exactly, for S3."""
    raw = (path or "").strip()
    lowered = raw.lower()
    for scheme in _S3_SCHEMES:
        if lowered.startswith(scheme):
            rest = raw[len(scheme):]
            break
    else:
        raise ValueError("path must start with s3://")
    bucket, _, folder = rest.partition("/")
    if not _S3_BUCKET_RE.match(bucket):
        raise ValueError(f"'{bucket}' is not a valid bucket name")
    return bucket, normalize_prefix(folder)


def format_s3_path(bucket: str, prefix: str = "") -> str:
    """Canonical display/storage form: s3://bucket or s3://bucket/folder (no trailing slash)."""
    prefix = normalize_prefix(prefix).rstrip("/")
    return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
