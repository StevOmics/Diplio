"""Pure index-file builder for the v2 backup pipeline (docs/cfa-spec.md section 5).

Turns one packer.PackedArchive into the section 5 index dict, ready for
json.dumps. This module does not write the file, upload it, or call
json.dumps itself - step 5 owns all of that.

No imports from app.models/app.db/app.gcs/app.config: like packer.py, this
is pure logic over its arguments only, so it stays in the dependency-free
test suite.
"""

from datetime import datetime, timezone
from typing import Callable

from app.packer import PackedArchive

INDEX_VERSION = 1


def rfc3339(dt: datetime) -> str:
    """Render an aware datetime as spec section 5's timestamp format,
    "%Y-%m-%dT%H:%M:%SZ" in UTC, whole seconds. Raises ValueError on a naive
    datetime rather than guessing its zone."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"rfc3339 requires an aware datetime, got naive {dt!r}")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _with_trailing_slash(prefix: str | None) -> str:
    """Step 01's normalize_prefix already guarantees a stored prefix ends in
    '/' and has no leading '/', but this module doesn't trust that a caller
    passed a normalized value - append a trailing slash if one is missing."""
    if not prefix:
        return ""
    return prefix if prefix.endswith("/") else prefix + "/"


def archive_object_key(archive_id: str, prefix: str | None) -> str:
    """<prefix><archive_id>.tar, bucket-relative. Deviates from spec section
    4's <prefix>archives/<archive_id>.tar - see docs/backup-plan/DEVIATIONS.md
    D12: the index file is now a same-directory, same-basename sidecar
    instead of living in a separate index/ folder, so browsing the bucket
    shows each archive right next to its own index."""
    return f"{_with_trailing_slash(prefix)}{archive_id}.tar"


def index_object_key(archive_id: str, prefix: str | None) -> str:
    """<prefix><archive_id>.json, bucket-relative - see D12."""
    return f"{_with_trailing_slash(prefix)}{archive_id}.json"


def index_key_for_archive_path(archive_path: str, archive_id: str) -> str:
    """Derives an archive's index key from its own .tar object key, handling
    both the current sidecar scheme and the older archives/+index/ scheme
    (D12) - both can coexist in one archive after this change, since it only
    affects newly-uploaded archives, not ones already in the bucket. Used by
    restore/verify, which only have the archive's recorded path to work from."""
    suffix = f"{archive_id}.tar"
    base_prefix = archive_path[: -len(suffix)] if archive_path.endswith(suffix) else archive_path.rsplit("/", 1)[0] + "/"
    if base_prefix.endswith("archives/"):
        library_prefix = base_prefix[: -len("archives/")]
        return f"{library_prefix}index/{archive_id}.json"
    return f"{base_prefix}{archive_id}.json"


def build_index(
    archive: PackedArchive,
    *,
    prefix: str | None,
    created_at: datetime,
    path_encryptor: Callable[[str, list[str]], str] | None = None,
    sha256_encryptor: Callable[[str, str], str] | None = None,
    key_check: str | None = None,
) -> dict:
    """Build the spec section 5 index dict for one sealed archive.

    Returns a plain dict of str/int/list/dict only - JSON-native throughout,
    with no Path, datetime, or dataclass instance anywhere in the result.

    With path_encryptor set (encrypted backups), each entry carries
    "paths_enc" - the encryptor's sealed token for (sha256, paths) - instead
    of a plaintext "paths" list, and the index is marked "encrypted": true.
    Nothing else changes: sizes and offsets stay readable.

    Entries are keyed by member.content_id (a keyed HMAC over sha256, see
    encryption.derive_content_id) rather than the real sha256 - a plain
    unkeyed hash in a GCS-visible index would let anyone with a candidate
    file confirm we hold a copy of it without ever touching the bucket's
    contents. Every member of a given archive is expected to carry a
    content_id or none at all (backup_run.py computes it for the whole run
    from one master key) - a mix would mean a caller bug, not a legitimate
    case. When content_id is absent throughout (no master key available for
    this run), entries fall back to the real sha256 as the key, matching
    every archive built before this scheme existed - see
    docs/backup-plan/DEVIATIONS.md.

    When content_id keying is in effect, sha256_encryptor (required in that
    case - backup_run.py always supplies it alongside path_encryptor) seals
    the real sha256 into each entry's "sha256_enc", so bucket_inventory.py's
    database-independent discovery can still recover a file's true identity
    with the master key alone - the whole reason content_id exists is that
    the real sha256 can't be recovered any other way once it's no longer the
    dict key.

    key_check (encryption.derive_key_check), when given, is stamped once per
    archive alongside key_scheme - a short verifier that lets a candidate
    password be confirmed against this archive directly, without a database
    and without trial-decrypting a real entry. Only meaningful alongside
    content-id keying; ignored when has_content_id is False.
    """
    has_content_id = any(m.content_id is not None for m in archive.members)
    if has_content_id and not all(m.content_id is not None for m in archive.members):
        raise ValueError(
            f"archive {archive.archive_id!r} has a mix of members with and without "
            "content_id - every member of a run should share one master key"
        )

    files: dict[str, dict] = {}
    for member in archive.members:
        key = member.content_id if has_content_id else member.sha256
        if key in files:
            # Step 03 (packer.pack) deduplicates by hash before building any
            # archive, so two members of one archive sharing a key means a
            # packer bug, not a legitimate case - overwriting the dict key
            # here would silently lose a file from the index.
            raise ValueError(
                f"duplicate index key {key!r} within archive {archive.archive_id!r} "
                "(packer.pack should already dedupe by hash)"
            )

        mtime = datetime.fromtimestamp(member.mtime_ns // 1_000_000_000, tz=timezone.utc)
        entry: dict = {
            # The whole file's size, always - even on a part entry, because
            # section 5 keys entries by the whole file's hash so that looking
            # one up across index files finds all of its parts.
            #
            # Consequence for restore (step 6): section 5 carries no member
            # byte-length. For clump and single members `size` *is* the
            # member's length, so a ranged read of `size` bytes at `offset`
            # is correct. For a part member it is not - `size` is the whole
            # file. A part archive holds exactly one member, so restore
            # downloads the whole part object instead of ranged-reading into
            # it. Do not use `size` as a read length for a part.
            "size": member.size_bytes,
            "mtime": rfc3339(mtime),
            "member": member.member_name,
            "offset": member.offset,
        }
        if path_encryptor is not None:
            entry["paths_enc"] = path_encryptor(member.sha256, list(member.paths))
        else:
            entry["paths"] = list(member.paths)
        # Section 5: a part entry "also carries" part/parts - their absence
        # is the normal (clump/single) case, so they're omitted rather than
        # emitted as None.
        # Absent means the member's bytes are the file as-is; "gzip" means
        # they are a gzip of it (and gunzip comes after decrypt on restore).
        if member.compression is not None:
            entry["compression"] = member.compression
        if member.part is not None:
            entry["part"] = member.part
            entry["parts"] = member.part_count
        if has_content_id:
            if sha256_encryptor is None:
                raise ValueError(
                    f"archive {archive.archive_id!r} is content-id-keyed but no sha256_encryptor "
                    "was given - the real sha256 would be unrecoverable by bucket_inventory.py"
                )
            entry["sha256_enc"] = sha256_encryptor(key, member.sha256)

        files[key] = entry

    index = {
        "v": INDEX_VERSION,
        "archive_id": archive.archive_id,
        "object": archive_object_key(archive.archive_id, prefix),
        "type": archive.archive_type,
        "created_at": rfc3339(created_at),
        "size": archive.size_bytes,
        "files": files,
    }
    if has_content_id:
        # Absence of this field is the unambiguous signal that an index
        # predates content-id keying and its "files" keys are real sha256
        # hex - every archive ever written before this field existed lacks
        # it by construction. Restore/verify (tasks.py) branch on this.
        index["key_scheme"] = "content_id_v1"
        if key_check is not None:
            index["key_check"] = key_check
    if path_encryptor is not None:
        index["encrypted"] = True
    return index
