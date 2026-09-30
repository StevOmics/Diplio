import hashlib
import json
import logging
import tarfile
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from google.api_core.exceptions import NotFound

from app.celery_app import app
from app.compression import (
    COMPRESSION_GZIP,
    crc32_and_size,
    decompress_prefix,
    gunzip_file,
    gzip_trailer,
    is_gzip_header,
)
from app.backup_index import index_key_for_archive_path
from app.db import SessionLocal
from app.encryption import (
    BLOB_HEADER_SIZE,
    GCM_TAG_SIZE,
    blob_size_for,
    decrypt_blob,
    decrypt_blob_chunk,
    derive_content_id,
    derive_file_key,
    derive_master_key,
    parse_blob_header,
)
from app.fingerprint import sha256_file
from app.models import (
    BackupArchive,
    BackupEncryptionConfig,
    BackupKeyVersion,
    BackupRecord,
    BackupRecordArchive,
    CloudStorageConfig,
    MediaFile,
    StorageLocation,
)
from app.storage import CountingBackend, backend_for_destination

logger = logging.getLogger(__name__)

CHUNK_SIZE = 8 * 1024 * 1024


def _get_master_key(db) -> bytes | None:
    config = db.query(BackupEncryptionConfig).first()
    if not config or not config.enabled or not config.password or not config.kdf_salt:
        return None
    return derive_master_key(config.password, bytes.fromhex(config.kdf_salt))


def _master_key_for_archive(db, archive: BackupArchive) -> bytes | None:
    """The master key that encrypted a v2 archive: its recorded key version
    (so a rotated-out passphrase still decrypts old backups), else the current
    key for archives that predate key versions."""
    if archive.key_version_id is not None:
        version = db.get(BackupKeyVersion, archive.key_version_id)
        if version:
            return derive_master_key(version.password, bytes.fromhex(version.kdf_salt))
    return _get_master_key(db)


def _get_cloud_storage_config(db) -> CloudStorageConfig | None:
    return db.query(CloudStorageConfig).first()


def _index_lookup_key(db, archive: BackupArchive, index: dict, sha256: str) -> str | None:
    """The key sha256's entry sits under in `index`'s "files" dict. A legacy
    index (no "key_scheme" field - every archive written before this scheme
    existed, e.g. an already-backed-up library) is keyed by the real sha256
    directly. A newer one is keyed by a keyed HMAC (encryption.derive_content_id,
    see docs/backup-plan/DEVIATIONS.md) so a candidate file can't be hashed
    and checked against our archives without the master key - derived with
    whichever key version actually wrote *this* archive (archive.key_version_id,
    fixed at write time), never the currently-active one, so a later rotation
    never makes an old archive's entries look missing. Returns None only when
    no master key is available at all to derive it - already an unusable
    state surfaced elsewhere (_refuse_if_unusable), not corruption."""
    if index.get("key_scheme") != "content_id_v1":
        return sha256
    master_key = _master_key_for_archive(db, archive)
    if not master_key:
        return None
    return derive_content_id(master_key, sha256)


# --- Split/clump support -----------------------------------------------------
#
# Both features reduce to the same two primitives: materialize a fully-formed
# local plaintext file representing one "unit" (a whole file, one split part,
# or one clump's concatenated body), store it at the destination
# (_store_plaintext_as_archive), and later read a unit back
# (_materialize_archive). Splitting a file writes N archives - one wholly
# dedicated to each part - linked to one BackupRecord. Clumping several files
# writes one archive shared by several BackupRecords, each pointing at its own
# byte range within it (BackupRecordArchive.archive_offset/archive_length).


def _v2_ledger_parts(
    db, media_file_id: int, destination_storage_location_id: int
) -> tuple[BackupRecord, list[tuple[BackupRecordArchive, BackupArchive]]] | None:
    """The v2 (tar-format) equivalent of _ledger_parts, above - kept separate
    rather than folded into it because the two formats' archives share the
    same tables (docs/cfa-spec.md section 6.6, no new tables) and are told
    apart only by BackupArchive.archive_id: NULL on every legacy row, set on
    every v2 row (see the comment on that column in models.py). Returns None
    (not []) when there's nothing to restore from - the call site reads as a
    single `if`.

    An archive with indexed_at IS NULL is incomplete and is excluded (section
    6's last line) - in practice run_backup always sets indexed_at in the
    same transaction that creates the row, so this should never actually
    trigger, but the spec is explicit that an unindexed archive must not be
    trusted by restore.
    """
    record = (
        db.query(BackupRecord)
        .filter_by(media_file_id=media_file_id, destination_storage_location_id=destination_storage_location_id)
        .one_or_none()
    )
    if not record:
        return None
    rows = (
        db.query(BackupRecordArchive, BackupArchive)
        .join(BackupArchive, BackupArchive.id == BackupRecordArchive.backup_archive_id)
        .filter(
            BackupRecordArchive.backup_record_id == record.id,
            BackupArchive.archive_id.isnot(None),
            BackupArchive.indexed_at.isnot(None),
        )
        .order_by(BackupRecordArchive.part_index)
        .all()
    )
    if not rows:
        return None
    return record, rows


def _restore_v2_member_bytes(backend, archive: BackupArchive, link: BackupRecordArchive, tmp_dir: Path) -> bytes:
    """One member's plain-file bytes, read the way its archive_type requires
    (docs/cfa-spec.md section 5/7 - see docs/backup-plan/steps/07-restore.md
    for why the two archive_type branches differ):

    - clump/single: a tar stores each member's raw data contiguously
      starting at its header's data offset, so a ranged read of exactly
      [archive_offset, archive_offset + archive_length) *is* the plain
      file's bytes - no tar parsing, and (for a clump) no need to touch the
      archive's other members.
    - part: section 5's index carries no member length (`size` is the whole
      file, not this part's byte length - see backup_index.py), and restore
      does not trust archive_length for the same reason either. A part
      archive holds exactly one member, so the whole object is downloaded
      and tarfile's own header parsing determines the exact payload length.
    """
    if archive.archive_type == "part":
        local_path = tmp_dir / f"{archive.archive_id}.tar"
        backend.download(archive.path, local_path)
        with tarfile.open(local_path) as tf:
            members = tf.getmembers()
            if len(members) != 1:
                raise RuntimeError(
                    f"part archive {archive.archive_id!r} has {len(members)} tar members, expected exactly 1"
                )
            extracted = tf.extractfile(members[0])
            return extracted.read()

    return backend.read_range(archive.path, link.archive_offset, link.archive_length)


# What decoding stored bytes can raise: a bad GCM tag / wrong key, a
# structurally bad blob, or a corrupt / truncated gzip stream.
_DECODE_ERRORS = (InvalidTag, ValueError, OSError, EOFError, zlib.error)


def _unwrap_stored_bytes(assembled: Path, dest: Path, tmp_dir: Path, *, file_key: bytes | None, compressed: bool) -> None:
    """The read-side inverse of the write pipeline (gzip -> encrypt -> pack):
    a file's stored bytes -> [decrypt] -> [gunzip] -> dest. Raises one of
    _DECODE_ERRORS if the bytes don't decode; callers decide what that means
    (restore: an error, verify: "mismatch")."""
    current = assembled
    if file_key is not None:
        decrypted = tmp_dir / "decrypted.out" if compressed else dest
        decrypt_blob(current, decrypted, file_key)
        current = decrypted
    if compressed:
        gunzip_file(current, dest)


def _restore_v2(
    db,
    media_file: MediaFile,
    record: BackupRecord,
    parts: list[tuple[BackupRecordArchive, BackupArchive]],
    output_path: Path,
    backend,
    on_progress=None,
) -> int:
    """Reconstructs output_path from a v2 ledger (docs/cfa-spec.md section 7):
    fetch each part's bytes in order, write and hash them incrementally so a
    multi-part split file never needs its whole reconstructed size held in
    memory at once, then verify the combined SHA-256 against the recorded
    one before moving the result into place. A verification failure - or a
    record with no sha256 to check against - raises rather than writing a
    file MediaBridge cannot vouch for; it never treats a missing hash as
    "nothing to verify".

    on_progress(bytes_fetched_so_far) is called as each part arrives. Returns
    the number of bytes fetched from the archive."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_final = output_path.with_name(output_path.name + ".mbcopy")

    encrypted = parts[0][1].encrypted
    compressed = record.compression == COMPRESSION_GZIP
    staged = encrypted or compressed
    hasher = hashlib.sha256()
    copied = 0
    with tempfile.TemporaryDirectory(prefix="mb-restore-") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        # Encrypted and/or gzipped backups: the parts concatenate to one blob
        # (ciphertext, or a gzip stream), which is then decoded; plain
        # as-is ones are written straight out.
        assembled = tmp_dir / "assembled.blob" if staged else tmp_final
        with assembled.open("wb") as out:
            for link, archive in parts:
                chunk = _restore_v2_member_bytes(backend, archive, link, tmp_dir)
                if not staged:
                    hasher.update(chunk)
                out.write(chunk)
                copied += len(chunk)
                if on_progress:
                    on_progress(copied)

        if staged:
            file_key = None
            if encrypted:
                if not record.sha256:
                    raise RuntimeError(f"cannot decrypt {media_file.path!r}: record has no sha256 to derive its key from")
                master_key = _master_key_for_archive(db, parts[0][1])
                if not master_key:
                    raise RuntimeError("backup is encrypted but no backup password is configured")
                file_key = derive_file_key(master_key, record.sha256)
            try:
                _unwrap_stored_bytes(assembled, tmp_final, tmp_dir, file_key=file_key, compressed=compressed)
            except _DECODE_ERRORS as exc:
                tmp_final.unlink(missing_ok=True)
                what = "decryption" if encrypted else "decompression"
                raise RuntimeError(
                    f"{what} of {media_file.path!r} failed: {exc or 'wrong key or corrupt data'}"
                ) from exc
            with tmp_final.open("rb") as f:
                while True:
                    block = f.read(CHUNK_SIZE)
                    if not block:
                        break
                    hasher.update(block)

    digest = hasher.hexdigest()
    if not record.sha256 or digest != record.sha256:
        tmp_final.unlink(missing_ok=True)
        raise RuntimeError(
            f"restored content for {media_file.path!r} failed sha256 verification "
            f"(expected {record.sha256!r}, got {digest!r})"
        )

    tmp_final.replace(output_path)
    return copied


# --- verify: cheap by construction ---------------------------------------------
#
# A shallow verify is priced in cloud requests and bytes read back (on
# Nearline/Coldline/Archive buckets every byte read is billed on top of the
# request). So it is built around the archive, not the file:
#   - an archive's object metadata (size + CRC32C - which already proves every
#     byte at rest is unchanged since upload) is fetched once per batch, by one
#     listing per ~1000 objects when many archives are involved;
#   - an archive's index file is downloaded once per batch, however many of its
#     files are being verified;
#   - the small data samples wanted from one archive are merged into as few
#     ranged reads as possible (a clump of small files is one read);
#   - data is only sampled where it is cheap (see VERIFY_SAMPLE_BUDGET_BYTES).

# Plain (unencrypted) files: how much of the start and of the end to read.
VERIFY_SAMPLE_BYTES = 1024 * 1024
# Encrypted files can only be checked a whole chunk at a time (a chunk is the
# smallest unit AES-GCM authenticates), and big files' chunks are big. A
# shallow verify therefore decodes a sample only for files whose stored size is
# within this budget; larger ones rely on the object CRC32C + index checks
# (and Deep verify reads everything).
VERIFY_SAMPLE_BUDGET_BYTES = 4 * 1024 * 1024
# Wanted ranges in one archive closer than this are read as one request.
VERIFY_RANGE_MERGE_GAP = 256 * 1024
# Rough ceiling on sample bytes held in memory while verifying a window of files.
VERIFY_WINDOW_BYTES = 128 * 1024 * 1024
VERIFY_WINDOW_FILES = 500

_BAD_INDEX = object()  # an index file that exists but doesn't parse
_NO_SAMPLE = object()  # sampling deliberately skipped (too costly for a shallow verify)


def _stored_pieces(parts, start: int, length: int) -> list[tuple[str, int, int]]:
    """(object key, physical offset, length) pieces covering `length` bytes at
    logical offset `start` of a file's stored bytes (its parts' members
    concatenated)."""
    pieces = []
    part_start = 0
    end = start + length
    for link, archive in parts:
        part_end = part_start + link.archive_length
        lo, hi = max(start, part_start), min(end, part_end)
        if lo < hi:
            pieces.append((archive.path, link.archive_offset + (lo - part_start), hi - lo))
        part_start = part_end
    return pieces


def _wanted_ranges(parts) -> list[tuple[str, int, int]]:
    """The data a shallow verify of this file will want, known from the ledger
    alone (so it can be fetched ahead of time, merged with its neighbours').
    Nothing for files sampling is skipped on."""
    total = sum(link.archive_length for link, _ in parts)
    if parts[0][1].encrypted:
        if total > BLOB_HEADER_SIZE + GCM_TAG_SIZE + VERIFY_SAMPLE_BUDGET_BYTES:
            return []
        return _stored_pieces(parts, 0, total)  # header + every chunk of a small blob
    if total <= 2 * VERIFY_SAMPLE_BYTES:
        return _stored_pieces(parts, 0, total)
    return _stored_pieces(parts, 0, VERIFY_SAMPLE_BYTES) + _stored_pieces(parts, total - VERIFY_SAMPLE_BYTES, VERIFY_SAMPLE_BYTES)


class _RangeCache:
    """A read-through stand-in for a backend's read_range: ranges registered
    with want() are fetched by prefetch() - merged so an archive's nearby
    ranges cost one request - and later reads inside a fetched span are served
    from memory. Anything else falls through to a direct ranged read."""

    def __init__(self, backend):
        self._backend = backend
        self._wanted: dict[str, list[tuple[int, int]]] = {}
        self._spans: dict[str, list[tuple[int, bytes]]] = {}

    def want(self, key: str, start: int, length: int) -> None:
        if length > 0:
            self._wanted.setdefault(key, []).append((start, start + length))

    def prefetch(self, skip=None) -> None:
        """Reads the wanted spans. `skip(key)` excludes objects already known
        to be unusable (missing / wrong size), so a bad archive is reported
        by the caller rather than crashing the whole batch here. A failed
        span is simply not cached; the later direct read reports it."""
        for key, ranges in self._wanted.items():
            if skip is not None and skip(key):
                continue
            merged: list[list[int]] = []
            for lo, hi in sorted(ranges):
                if merged and lo - merged[-1][1] <= VERIFY_RANGE_MERGE_GAP:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            for lo, hi in merged:
                try:
                    self._spans.setdefault(key, []).append((lo, self._backend.read_range(key, lo, hi - lo)))
                except Exception:  # noqa: BLE001 - see docstring
                    logger.debug("verify prefetch of %s[%d:%d] failed", key, lo, hi, exc_info=True)
        self._wanted.clear()

    def clear(self) -> None:
        self._wanted.clear()
        self._spans.clear()

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        for lo, data in self._spans.get(key, ()):
            if lo <= offset and offset + length <= lo + len(data):
                return data[offset - lo : offset - lo + length]
        return self._backend.read_range(key, offset, length)


class _VerifyContext:
    """What a batch of verifies shares: each archive's object metadata and
    index file (fetched once), and the merged data reads."""

    def __init__(self, backend):
        self.backend = backend
        self.ranges = _RangeCache(backend)
        self._stats: dict[str, object] = {}
        self._indexes: dict[str, object] = {}

    def prime_stats(self, keys_by_prefix: dict[str, set[str]], total_by_prefix: dict[str, int]) -> None:
        """Fetch object metadata for the wanted archive keys. Per prefix: one
        listing (a request per 1000 objects, metadata only) when that is
        cheaper than a request per wanted object, else per-object stats."""
        for prefix, keys in keys_by_prefix.items():
            listing_requests = -(-max(total_by_prefix.get(prefix, len(keys)), 1) // 1000)
            if len(keys) > listing_requests:
                listed = self.backend.list_stats(prefix)
                for key in keys:
                    self._stats[key] = listed.get(key)
            else:
                for key in keys:
                    self._stats[key] = self.backend.stat_or_none(key)

    def stat(self, key: str):
        if key not in self._stats:
            self._stats[key] = self.backend.stat_or_none(key)
        return self._stats[key]

    def index(self, key: str):
        """The parsed index file, None if missing, _BAD_INDEX if unparsable."""
        if key not in self._indexes:
            raw = self.backend.read_object(key)
            if raw is None:
                self._indexes[key] = None
            else:
                try:
                    self._indexes[key] = json.loads(raw)
                except ValueError:
                    self._indexes[key] = _BAD_INDEX
        return self._indexes[key]


def _read_stored_range(backend, parts, start: int, length: int) -> bytes:
    """`length` bytes at logical offset `start` of a file's stored bytes, by
    ranged reads (`backend` may be a _RangeCache) - never a whole download,
    even for a split file."""
    out = b"".join(backend.read_range(key, off, n) for key, off, n in _stored_pieces(parts, start, length))
    if len(out) != length:
        raise ValueError("stored bytes are shorter than the ledger says")
    return out


def _sample_stored_ends(db, record: BackupRecord, parts, backend):
    """(head, tail, stream_size) of a file's stored bytes with any encryption
    removed - i.e. the file's own bytes, or its gzip - or None if the bucket
    copy is not sound (bad header, wrong total length, or a chunk that fails
    authentication), or _NO_SAMPLE when sampling is skipped as too costly. For
    an encrypted blob head/tail are its first and last chunk, decrypted;
    chunks are independent, so this proves the key and format work without
    reading the middle."""
    total = sum(link.archive_length for link, _ in parts)
    if not parts[0][1].encrypted:
        n = min(VERIFY_SAMPLE_BYTES, total)
        return _read_stored_range(backend, parts, 0, n), _read_stored_range(backend, parts, total - n, n), total

    if total > BLOB_HEADER_SIZE + GCM_TAG_SIZE + VERIFY_SAMPLE_BUDGET_BYTES:
        return _NO_SAMPLE
    master_key = _master_key_for_archive(db, parts[0][1])
    if not master_key or not record.sha256:
        raise RuntimeError("backup is encrypted but no backup password is configured")
    file_key = derive_file_key(master_key, record.sha256)
    header = _read_stored_range(backend, parts, 0, BLOB_HEADER_SIZE)
    try:
        _, chunk_size, plain_size = parse_blob_header(header)
        if total != blob_size_for(plain_size, chunk_size):
            return None
        if plain_size == 0:
            return b"", b"", 0
        chunks = -(-plain_size // chunk_size)
        step = chunk_size + GCM_TAG_SIZE

        def chunk(i: int) -> bytes:
            plain_len = min(chunk_size, plain_size - i * chunk_size)
            cipher = _read_stored_range(backend, parts, BLOB_HEADER_SIZE + i * step, plain_len + GCM_TAG_SIZE)
            return decrypt_blob_chunk(file_key, header, i, cipher)

        head = chunk(0)
        tail = head if chunks == 1 else chunk(chunks - 1)
    except (InvalidTag, ValueError):
        return None
    return head, tail, plain_size


def _sampled_content_ok(head: bytes, tail: bytes, stream_size: int, compressed: bool, local: Path | None) -> bool:
    """Do the sampled ends look right? Always checks the structure (gzip
    magic/trailer); if `local` is the file as backed up, also that the samples
    agree with it. Never reads the middle of the stored data."""
    if compressed:
        if not is_gzip_header(head) or stream_size < 18:
            return False
        try:
            crc, isize = gzip_trailer(tail)
            prefix = decompress_prefix(head)
        except (ValueError, zlib.error):
            return False
        if local is None:
            return True
        local_crc, local_size = crc32_and_size(local)
        if (crc, isize) != (local_crc, local_size & 0xFFFFFFFF):
            return False
        with local.open("rb") as f:
            return f.read(len(prefix)) == prefix
    if local is None:
        return True
    if local.stat().st_size != stream_size:
        return False
    with local.open("rb") as f:
        if f.read(len(head)) != head:
            return False
        f.seek(stream_size - len(tail))
        return f.read(len(tail)) == tail


def _verify_v2(
    db,
    media_file: MediaFile,
    record: BackupRecord,
    parts: list[tuple[BackupRecordArchive, BackupArchive]],
    backend,
    *,
    deep: bool = False,
    ctx: "_VerifyContext | None" = None,
) -> str:
    """Checks that a v2-backed file really is in the bucket, intact, and still
    matches the local file. Returns a verify_status:

    - "missing":  an archive object (or its index file) is gone from the bucket.
    - "mismatch": an object's size/CRC32C differ from what was recorded at
                  upload, its index file disagrees with the database, or the
                  stored bytes don't decode / don't match the file.
    - "changed":  the bucket copy is intact, but the local file has since been
                  modified, so it is no longer what was backed up.
    - "match":    the bucket copy is intact and identical to the local file
                  (or the local file is gone, which is when you'd restore it).

    Two depths, sharing steps 1 and 3:
    - sampled (default): reads only the start and end of the stored data
      (see _sample_stored_ends). The size + CRC32C check already covers every
      byte of each object at rest (GCS computes CRC32C server-side), so this
      adds proof that the ends decode (right key, valid gzip) and, when the
      local file is unchanged, agree with it. It cannot see a corrupt middle
      that leaves the object's CRC32C intact - i.e. a bug on our side that
      wrote bad bytes and recorded their CRC.
    - deep: downloads and decodes everything and compares the SHA-256.

    Unlike restore this never writes to the file's real path."""
    if not record.sha256:
        return "mismatch"
    compressed = record.compression == COMPRESSION_GZIP
    ctx = ctx or _VerifyContext(backend)  # a batch passes a shared one

    # 1. Cheap metadata check on every object involved, without downloading.
    for archive in {archive.id: archive for _, archive in parts}.values():
        stat = ctx.stat(archive.path)
        if stat is None:
            return "missing"
        if stat.size != archive.size_bytes or (archive.crc32c and stat.crc32c != archive.crc32c):
            return "mismatch"

    # 1b. Each archive's plaintext index file must exist and agree with the
    # database (it is what a recovery without the database would rely on).
    for link, archive in parts:
        index = ctx.index(index_key_for_archive_path(archive.path, archive.archive_id))
        if index is None:
            return "missing"
        try:
            if index is _BAD_INDEX:
                return "mismatch"
            lookup_key = _index_lookup_key(db, archive, index, record.sha256)
            if lookup_key is None:
                return "mismatch"
            entry = index["files"][lookup_key]
        except (KeyError, TypeError):
            return "mismatch"
        if index.get("archive_id") != archive.archive_id or index.get("object") != archive.path:
            return "mismatch"
        if archive.archive_type != "part" and entry.get("offset") != link.archive_offset:
            return "mismatch"
        if entry.get("compression") != record.compression:
            return "mismatch"

    # Is the local file still what was backed up? (Needed to judge samples.)
    local = Path(media_file.path)
    local_present = local.is_file()
    local_same = local_present and sha256_file(local) == record.sha256

    if deep:
        # 2. Read everything back, decode it, and hash the plain bytes.
        encrypted = parts[0][1].encrypted
        staged = encrypted or compressed
        hasher = hashlib.sha256()
        with tempfile.TemporaryDirectory(prefix="mb-verify-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            assembled = tmp_dir / "assembled.blob"
            with assembled.open("wb") as out:
                for link, archive in parts:
                    chunk = _restore_v2_member_bytes(backend, archive, link, tmp_dir)
                    if not staged:
                        hasher.update(chunk)
                    out.write(chunk)
            if staged:
                file_key = None
                if encrypted:
                    master_key = _master_key_for_archive(db, parts[0][1])
                    if not master_key:
                        raise RuntimeError("backup is encrypted but no backup password is configured")
                    file_key = derive_file_key(master_key, record.sha256)
                plain = tmp_dir / "plain.out"
                try:
                    _unwrap_stored_bytes(assembled, plain, tmp_dir, file_key=file_key, compressed=compressed)
                except _DECODE_ERRORS:
                    return "mismatch"
                with plain.open("rb") as f:
                    while block := f.read(CHUNK_SIZE):
                        hasher.update(block)
        if hasher.hexdigest() != record.sha256:
            return "mismatch"
    else:
        # 2. Sample the ends of the stored data.
        try:
            sampled = _sample_stored_ends(db, record, parts, ctx.ranges)
        except (FileNotFoundError, NotFound):
            return "missing"  # deleted between the metadata check and the read
        except ValueError:
            return "mismatch"
        if sampled is None:
            return "mismatch"
        if sampled is not _NO_SAMPLE:
            head, tail, stream_size = sampled
            if not _sampled_content_ok(head, tail, stream_size, compressed, local if local_same else None):
                return "mismatch"

    # 3. The bucket copy is good; is it still the local file's content?
    if local_present and not local_same:
        return "changed"
    return "match"


def _verify_v2_batch(
    db, media_file_ids: list[int], destination_id: int, backend, *, deep: bool = False
) -> dict[int, str]:
    """Verifies many files at one archive, sharing everything shareable so the
    cloud cost tracks the number of *archives* touched, not files (see the
    "verify: cheap by construction" notes above). Files are handled in
    archive order, in windows, so a clump's members are read together and
    memory stays bounded. Writes each file's verify_status/verified_at."""
    items = []
    for media_file_id in media_file_ids:
        media_file = db.get(MediaFile, media_file_id)
        v2 = _v2_ledger_parts(db, media_file_id, destination_id)
        if media_file and v2:
            items.append((media_file, v2[0], v2[1]))
    # Archive order (then offset): members of one clump end up adjacent.
    items.sort(key=lambda it: (it[2][0][1].id, it[2][0][0].archive_offset))

    ctx = _VerifyContext(backend)
    keys_by_prefix: dict[str, set[str]] = {}
    for _, _, parts in items:
        for _, archive in parts:
            # The .tar's own containing folder - works for both the old
            # archives/ subfolder and the current sidecar scheme (D12),
            # since it's just "everything up to the last /", not assuming
            # either layout.
            prefix = archive.path.rsplit("/", 1)[0] + "/"
            keys_by_prefix.setdefault(prefix, set()).add(archive.path)
    total_by_prefix = {
        prefix: db.query(BackupArchive)
        .filter(BackupArchive.storage_location_id == destination_id, BackupArchive.path.like(prefix + "%"))
        .count()
        for prefix in keys_by_prefix
    }
    ctx.prime_stats(keys_by_prefix, total_by_prefix)

    results: dict[int, str] = {}
    window: list = []
    window_bytes = 0

    def flush_window() -> None:
        nonlocal window, window_bytes
        if not window:
            return
        if not deep:
            for _, _, parts in window:
                for key, start, length in _wanted_ranges(parts):
                    ctx.ranges.want(key, start, length)
            ctx.ranges.prefetch(skip=lambda key: ctx.stat(key) is None)
        for media_file, record, parts in window:
            record.verify_status = _verify_v2(db, media_file, record, parts, backend, deep=deep, ctx=ctx)
            record.verified_at = datetime.now(timezone.utc)
            results[media_file.id] = record.verify_status
        db.commit()
        ctx.ranges.clear()
        window, window_bytes = [], 0

    for item in items:
        cost = sum(n for _, _, n in _wanted_ranges(item[2])) if not deep else 0
        if window and (len(window) >= VERIFY_WINDOW_FILES or window_bytes + cost > VERIFY_WINDOW_BYTES):
            flush_window()
        window.append(item)
        window_bytes += cost
    flush_window()
    return results


@app.task(name="verify_v2_batch")
def verify_v2_batch(media_file_ids: list[int], destination_storage_location_id: int, deep: bool = False) -> None:
    """Verifies a batch of files' v2 backups at one archive (see
    _verify_v2_batch) and logs what it cost in cloud requests."""
    db = SessionLocal()
    try:
        destination = db.get(StorageLocation, destination_storage_location_id)
        if not destination:
            raise RuntimeError("cloud storage isn't configured")
        backend = CountingBackend(backend_for_destination(db, destination))
        results = _verify_v2_batch(db, media_file_ids, destination_storage_location_id, backend, deep=deep)
        counts: dict[str, int] = {}
        for status in results.values():
            counts[status] = counts.get(status, 0) + 1
        logger.info(
            "verify (%s): %d file(s) %s - cloud cost: %s",
            "deep" if deep else "shallow", len(results), counts, backend.summary(),
        )
    finally:
        db.close()


@app.task(name="verify_v2_backup")
def verify_v2_backup(media_file_id: int, destination_storage_location_id: int, deep: bool = False) -> None:
    """One file - kept for tasks already queued by an older web build; new
    callers send batches (verify_v2_batch)."""
    verify_v2_batch.run([media_file_id], destination_storage_location_id, deep)
