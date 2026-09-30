"""Cloud inventory: which objects in a bucket folder were created by
MediaBridge, with their storage tier, compared against what the database
says was uploaded. Recognizes two object layouts: the current sidecar
scheme, `<prefix><uuid>.tar` next to `<prefix><uuid>.json` (see
docs/backup-plan/DEVIATIONS.md D12), and the older `<prefix>archives/<uuid>.tar`
+ `<prefix>index/<uuid>.json` layout from before it - both can appear under
one prefix, since D12 only changed where *new* uploads land.

Pure logic over (key, size, storage_class) tuples - no GCS or database
imports - so it stays in the dependency-free test suite. Anything under the
prefix that isn't a MediaBridge object is ignored.
"""
import re
from dataclasses import dataclass, field

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Old-scheme patterns are tried first (they require the literal archives/ or
# index/ segment) - falling straight to a single "subfolder optional" regex
# would greedily swallow that segment into the prefix capture, since prefix
# is `(?:.*/)?` and the subfolder is otherwise optional, and then an old- and
# a new-scheme object would capture different prefixes for the same folder,
# breaking the (prefix, id) pairing below.
_ARCHIVE_RE_OLD = re.compile(rf"^(?P<prefix>(?:.*/)?)archives/(?P<id>{_UUID})\.tar$")
_ARCHIVE_RE_NEW = re.compile(rf"^(?P<prefix>(?:.*/)?)(?P<id>{_UUID})\.tar$")
_INDEX_RE_OLD = re.compile(rf"^(?P<prefix>(?:.*/)?)index/(?P<id>{_UUID})\.json$")
_INDEX_RE_NEW = re.compile(rf"^(?P<prefix>(?:.*/)?)(?P<id>{_UUID})\.json$")


def _match_archive(key: str):
    return _ARCHIVE_RE_OLD.match(key) or _ARCHIVE_RE_NEW.match(key)


def _match_index(key: str):
    return _INDEX_RE_OLD.match(key) or _INDEX_RE_NEW.match(key)


@dataclass
class InventoryReport:
    archive_count: int = 0
    total_bytes: int = 0
    # storage class (e.g. "ARCHIVE") -> [archive count, bytes]
    by_tier: dict[str, list[int]] = field(default_factory=dict)
    untracked: list[str] = field(default_factory=list)  # in the bucket, not in the database
    missing: list[str] = field(default_factory=list)  # in the database, not in the bucket
    orphan_index: list[str] = field(default_factory=list)  # index file with no archive object
    no_index: list[str] = field(default_factory=list)  # archive object with no index file


def build_report(objects: list[tuple[str, int, str | None]], known_keys: set[str]) -> InventoryReport:
    """objects: (key, size, storage_class) for everything listed under the
    archive's prefix. known_keys: archive object keys the database recorded."""
    index_ids: dict[tuple[str, str], str] = {}
    for key, _, _ in objects:
        m = _match_index(key)
        if m:
            index_ids[(m["prefix"], m["id"])] = key

    report = InventoryReport()
    found: set[str] = set()
    for key, size, storage_class in sorted(objects):
        m = _match_archive(key)
        if not m:
            continue
        found.add(key)
        report.archive_count += 1
        report.total_bytes += size
        tier = report.by_tier.setdefault(storage_class or "UNKNOWN", [0, 0])
        tier[0] += 1
        tier[1] += size
        if index_ids.pop((m["prefix"], m["id"]), None) is None:
            report.no_index.append(key)
        if key not in known_keys:
            report.untracked.append(key)
    report.orphan_index = sorted(index_ids.values())
    report.missing = sorted(known_keys - found)
    return report
