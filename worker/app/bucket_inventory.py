"""Reads what a backup already wrote directly out of a bucket, without any
database row to work from - the building block for portability
(docs/backup-plan/steps/18-portability.md, DEVIATIONS.md D13): a fresh (or
repointed) instance's database has never heard of this content, so instead
of the normal `_v2_ledger_parts` lookup, this lists every index file under a
prefix and reads them directly, mirroring spec section 7's index-file
fallback (`DEVIATIONS.md` D7) - not for "the database is unavailable" as D7
scoped it, but for "there has never been a database row for this content on
this instance" - same mechanism, different trigger.

Pure over its arguments - a StorageBackend, a prefix, and a path-resolving
callback - no app.db/app.models/crypto import, so it's testable against
tests/storage_double.LocalBackend with nothing running. worker/app/sync_run.py
owns the database and encryption-key side of this.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol


class _Backend(Protocol):
    def list_stats(self, prefix: str) -> dict: ...
    def read_object(self, key: str) -> bytes | None: ...
    def stat_or_none(self, key: str): ...


@dataclass(frozen=True)
class DiscoveredPart:
    """One archive's contribution to one file's content - shaped exactly like
    what tasks._restore_v2_member_bytes needs to extract it: for clump/single
    (archive_type != "part") a ranged read at (offset, size) within
    archive_path; for a part, the whole object is downloaded and tar-parsed
    (offset/size are the whole file's, per spec section 5, not the part's -
    same rule restore already follows, not used for the read itself)."""

    archive_path: str
    archive_id: str
    archive_type: str
    encrypted: bool
    offset: int
    size: int
    part: int | None


@dataclass(frozen=True)
class DiscoveredFile:
    """One piece of content found in the bucket, identified by its hash - the
    index-derived equivalent of a (BackupRecord, [(BackupRecordArchive,
    BackupArchive), ...]) pair, except there may be no MediaFile/BackupRecord
    for it yet on this instance."""

    sha256: str
    size_bytes: int
    mtime_ns: int
    compression: str | None
    # None when every index entry sealed its paths (paths_enc) and none of
    # them could be decrypted - the content is known to exist (hash, size,
    # where it's stored) but not what it's called or where it goes locally.
    paths: list[str] | None
    parts: list["DiscoveredPart"]  # ordered by `part` (clump/single: exactly one)


@dataclass
class DiscoveryResult:
    files: list[DiscoveredFile] = field(default_factory=list)
    # Listed under the prefix, ends in .json, but isn't a well-formed index.
    bad_index_keys: list[str] = field(default_factory=list)
    # Index file exists but its own archive object doesn't - an incomplete
    # or since-deleted upload; spec section 6's last line already treats an
    # archive with no index as unusable, this is the same rule the other way.
    dangling_index_keys: list[str] = field(default_factory=list)


# (sha256, entry, archive_id) -> real paths, or None if unresolvable (sealed
# and nothing could open it). sync_run.py's resolver also needs archive_id so
# it can cache "which key opens this archive" instead of retrying per file.
# Always receives the real sha256 (resolved via Sha256Resolver first for a
# content-id-keyed entry), never the raw index key.
PathResolver = Callable[[str, dict, str], "list[str] | None"]

# (content_id, entry, index) -> the real sha256, or None if unresolvable
# (sealed and nothing could open it). Only called for a content-id-keyed
# entry (see build_index's "key_scheme") - a legacy entry's real sha256 is
# already its own dict key, resolved with no key needed at all. Receives the
# whole index dict (not just archive_id) so a caller can read "key_check"
# off it to identify the right key without trial-decrypting an entry.
Sha256Resolver = Callable[[str, dict, dict], "str | None"]


def list_index_keys(backend: _Backend, prefix: str) -> list[str]:
    return sorted(key for key in backend.list_stats(prefix) if key.endswith(".json"))


def _parse_index_mtime_ns(mtime: str | None) -> int:
    if not mtime:
        return 0
    try:
        return int(datetime.strptime(mtime, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def discover(
    backend: _Backend, prefix: str, resolve_paths: PathResolver, resolve_sha256: Sha256Resolver | None = None
) -> DiscoveryResult:
    """Reads every index file under `prefix`, groups their entries by content
    hash (spec section 5: the hash is that file's identity - looking it up
    across index files finds all its parts, and any duplicate uploads of the
    same content), and returns what's there. Calls `resolve_paths` once per
    (file, index-entry) pair - sync_run.py's version caches per archive_id so
    a clump of many files only costs one decrypt attempt per key tried, not
    one per file.

    A content-id-keyed index (see build_index's "key_scheme") has no real
    sha256 to group by until `resolve_sha256` recovers one from the entry's
    sealed "sha256_enc" (docs/backup-plan/DEVIATIONS.md) - without it (or
    when it can't be resolved, e.g. the entry was sealed with a key this
    caller doesn't have), that entry has no identity at all and is dropped:
    there is nothing else in a content-id-keyed archive that reveals it. A
    legacy entry (no "key_scheme") needs no resolving at all - its dict key
    already is the real sha256, exactly as before this scheme existed."""
    result = DiscoveryResult()
    by_hash: dict[str, list[tuple[dict, dict]]] = {}

    for index_key in list_index_keys(backend, prefix):
        raw = backend.read_object(index_key)
        index = None
        if raw is not None:
            try:
                index = json.loads(raw)
            except ValueError:
                index = None
        if not isinstance(index, dict) or not isinstance(index.get("files"), dict) or not index.get("object") or not index.get("archive_id"):
            result.bad_index_keys.append(index_key)
            continue
        if backend.stat_or_none(index["object"]) is None:
            result.dangling_index_keys.append(index_key)
            continue
        content_id_keyed = index.get("key_scheme") == "content_id_v1"
        for key, entry in index["files"].items():
            if not isinstance(entry, dict):
                continue
            if content_id_keyed:
                if resolve_sha256 is None:
                    continue
                sha256 = resolve_sha256(key, entry, index)
                if sha256 is None:
                    continue
            else:
                sha256 = key
            by_hash.setdefault(sha256, []).append((entry, index))

    for sha256, entries in by_hash.items():
        first_entry, _ = entries[0]
        size, mtime, compression = first_entry.get("size"), first_entry.get("mtime"), first_entry.get("compression")
        if not isinstance(size, int):
            continue  # not a well-formed entry - nothing usable to build from

        paths: list[str] | None = None
        parts: list[DiscoveredPart] = []
        consistent = True
        for entry, index in entries:
            # Every entry sharing a hash must agree with the first one seen -
            # the hash IS the content's identity (spec section 5), so a
            # disagreement means a hash collision or a corrupt index, either
            # way not safe to guess between.
            if entry.get("size") != size or entry.get("mtime") != mtime or entry.get("compression") != compression:
                consistent = False
                break
            resolved = resolve_paths(sha256, entry, index["archive_id"])
            if resolved and paths is None:
                paths = resolved
            parts.append(
                DiscoveredPart(
                    archive_path=index["object"],
                    archive_id=index["archive_id"],
                    archive_type=index.get("type") or "single",
                    encrypted=bool(index.get("encrypted")),
                    offset=entry.get("offset") or 0,
                    size=size,
                    part=entry.get("part"),
                )
            )
        if not consistent or not parts:
            continue
        parts.sort(key=lambda p: p.part or 0)
        result.files.append(
            DiscoveredFile(
                sha256=sha256, size_bytes=size, mtime_ns=_parse_index_mtime_ns(mtime), compression=compression,
                paths=paths, parts=parts,
            )
        )
    return result
