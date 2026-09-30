"""Pure helpers for the v2 backup pipeline's settings (spec sizes, GCS prefix,
per-source exclude globs) - see docs/cfa-spec.md section 2 and
docs/backup-plan/steps/01-settings.md.

No imports from app.models or app.db: this module only normalizes/validates
form input, so it stays testable in the dependency-free default suite. Nothing
in the app reads the settings these helpers prepare yet - see the step doc.
"""
from __future__ import annotations

MIB = 1024 * 1024
GIB = 1024**3


def normalize_prefix(raw: str | None) -> str | None:
    """Strips whitespace and any leading slash, then guarantees exactly one
    trailing slash, so "server01", "/server01/", and "server01/" all land on
    "server01/". Empty, whitespace-only, or slash-only input normalizes to
    None, meaning "no prefix" (bucket root) - see CloudStorageConfig.prefix."""
    if raw is None:
        return None
    stripped = raw.strip().lstrip("/")
    if not stripped:
        return None
    return stripped if stripped.endswith("/") else stripped + "/"


def parse_exclude_globs(raw: str | None) -> list[str]:
    """Splits newline-separated glob patterns, strips each line, drops blank
    lines, and de-duplicates while preserving first-seen order."""
    if not raw:
        return []
    globs: list[str] = []
    for line in raw.splitlines():
        pattern = line.strip()
        if pattern and pattern not in globs:
            globs.append(pattern)
    return globs


def validate_archive_sizes(min_size_bytes: float, clump_size_bytes: float, max_size_bytes: float) -> str | None:
    """Validates the three spec sizes (docs/cfa-spec.md section 2) in the order the
    step spec lists, so a negative input always produces the "must be
    positive" message rather than a confusing downstream comparison error.
    Returns the exact (already "+"-encoded, matching the rest of this file's
    error-redirect style) message the route should redirect with, or None
    when the three sizes are valid together."""
    if min_size_bytes <= 0 or clump_size_bytes <= 0 or max_size_bytes <= 0:
        return "Archive+sizes+must+be+positive"
    if min_size_bytes >= clump_size_bytes:
        return "Min+size+must+be+smaller+than+the+clump+size"
    if clump_size_bytes > max_size_bytes:
        return "Clump+size+cannot+exceed+the+max+object+size"
    if max_size_bytes <= MIB:
        # spec section 3's split margin is max_size - 1 MiB, which must stay positive.
        return "Max+object+size+must+be+larger+than+1+MiB"
    return None


def parse_upload_limit(raw: str, measured_upload_mbps: float | None) -> tuple[float | None, str | None]:
    """Parses Settings > Cloud Storage's "Speed limit" field into
    (TransferConfig.max_upload_mbps, error) - exactly one is non-None. Blank
    input clears the override (returns (None, None)), meaning "use the
    automatic default" (worker/app/gcs.py:effective_upload_mbps). A set
    limit must be a positive number and, when a speed test is on record,
    can't exceed the raw measured upload speed - a "limit" above what the
    connection actually tested at isn't a limit."""
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        value = float(raw)
    except ValueError:
        return None, "Upload+limit+must+be+a+number"
    if value <= 0:
        return None, "Upload+limit+must+be+greater+than+0+Mbps"
    if measured_upload_mbps and value > measured_upload_mbps:
        return None, (
            "Upload+limit+can%27t+exceed+the+last+measured+speed+of+"
            f"{measured_upload_mbps:.1f}+Mbps+-+run+the+diagnostic+again+if+your+connection+has+improved"
        )
    return value, None
