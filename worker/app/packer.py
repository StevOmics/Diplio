"""Pure packing logic for the v2 backup pipeline (docs/cfa-spec.md section 3).

Classifies files by size, groups small ones into clump tars, splits oversized
ones into part tars, writes every archive to a caller-supplied temp directory,
and reports each member's byte offset within its own tar so step 6 can do a
ranged restore.

No imports from app.models/app.db/app.gcs/app.config: this module never talks
to the database or GCS, and the three size settings arrive as plain
arguments, not lookups. That keeps it in the dependency-free test suite and
importable from a plain `python -c` with only the standard library.

Three measured facts about Python's tarfile drive several choices below (see
docs/backup-plan/steps/03-packer.md for how they were confirmed):
  1. tarfile.addfile(ti, f) does copy.copy(ti) internally, so the TarInfo
     handed to addfile() never receives offset_data - it stays 0. Offsets
     must be read back by reopening the sealed tar and reading offset_data
     off of getmembers().
  2. ustar member names cap out at 100 characters, which real media paths
     exceed routinely; tarfile.PAX_FORMAT must be passed explicitly.
  3. tar pads to tarfile.RECORDSIZE (10240 bytes), worst-case ~13 KiB of
     overhead - comfortably inside the spec's 1 MiB split margin.
"""

import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

MIB = 1024 * 1024

CLUMP = "clump"
SINGLE = "single"
PART = "part"


@dataclass(frozen=True)
class FileToPack:
    source_path: Path  # absolute path to read bytes from
    rel_path: str  # path relative to its source root; the tar member name
    sha256: str
    size_bytes: int
    mtime_ns: int
    # When set, the tar member name is this (e.g. "<content_id>.file" for an
    # encrypted blob) instead of rel_path, so paths never appear in the tar.
    # rel_path is still what the index reports as the file's path.
    member_stem: str | None = None
    # How the bytes at source_path were transformed from the original file
    # before packing ("gzip", or None for as-is). Carried through to the
    # PackedMember so the index and database can record it.
    compression: str | None = None
    # Keyed HMAC identifier (encryption.derive_content_id(key, sha256)),
    # precomputed by the caller same as member_stem. This, not sha256, is
    # what backup_index.py keys the plaintext index by - sha256 must never
    # appear in a GCS-visible artifact. None only for legacy/no-master-key
    # call sites that still fall back to sha256 keying.
    content_id: str | None = None


@dataclass(frozen=True)
class PackedMember:
    sha256: str
    content_id: str | None
    paths: list[str]  # more than one when files in the run share a hash
    size_bytes: int  # the whole file's size, even for a part
    mtime_ns: int
    member_name: str
    offset: int  # byte offset of this member's data within the tar
    part: int | None  # 1-based; None for clump and single
    part_count: int | None  # None for clump and single
    compression: str | None = None  # "gzip" when the member's bytes are a gzip of the file


@dataclass(frozen=True)
class PackedArchive:
    archive_id: str  # uuid4, str(uuid.uuid4())
    archive_type: str  # "clump" | "single" | "part"
    local_path: Path  # dest_dir / f"{archive_id}.tar"
    size_bytes: int  # local_path.stat().st_size
    members: list[PackedMember]


@dataclass(frozen=True)
class _UniqueContent:
    """One deduplicated-by-hash content group from the input (spec section 5:
    "If two files in the same run share a hash, the content is stored once
    and paths lists both."). rel_path/source_path are the representative
    entry's (the one whose rel_path sorts first in the group); paths is every
    rel_path in the group, sorted."""

    sha256: str
    content_id: str | None
    rel_path: str
    paths: list[str]
    source_path: Path
    size_bytes: int
    mtime_ns: int
    member_stem: str | None = None
    compression: str | None = None

    @property
    def member_name(self) -> str:
        return self.member_stem or self.rel_path


def payload_ceiling(max_size: int) -> int:
    """The largest payload any single archive may hold, spec section 3's
    `max_size` less its 1 MiB tar-overhead margin.

    Section 3 applies that margin only to the split formula, but section 9
    requires that *no* uploaded object exceed max_size, and tar overhead does
    not care which archive type produced it. Measured: a file one byte under
    a 3 MiB max_size produces a 3,153,920-byte tar, 8,192 bytes over. So the
    same ceiling governs all three types - it is the single/part boundary,
    the split chunk size, and (with clump_size) the clump seal point.

    Deviation from section 3's table, recorded in docs/CHANGES.md: the table
    puts the single/part boundary at max_size, which section 9 then
    contradicts. Everything else about the table is unchanged.
    """
    ceiling = max_size - MIB
    if ceiling <= 0:
        raise ValueError(
            f"max_size ({max_size}) leaves no room after the 1 MiB tar-overhead margin"
        )
    return ceiling


def classify(size_bytes: int, *, min_size: int, max_size: int) -> str:
    """Spec section 3's table, with the single/part boundary at
    payload_ceiling(max_size) rather than max_size so the resulting tar
    cannot exceed max_size. `clump_size` is deliberately not a parameter
    here - it governs when a clump is sealed, not which archive type a file
    belongs to."""
    if size_bytes < min_size:
        return CLUMP
    if size_bytes <= payload_ceiling(max_size):
        return SINGLE
    return PART


def split_part_sizes(
    size_bytes: int, *, max_size: int, align_unit: int = 0, align_head: int = 0
) -> list[int]:
    """Spec section 3's split formula: k parts, balanced, sized off a chunk
    that leaves a 1 MiB margin per part for tar overhead.

    With align_unit set (encrypted blobs), parts are instead cut on chunk
    boundaries: a blob is align_head header bytes followed by align_unit-sized
    chunks, every part but the last holds as many whole chunks as fit under
    the ceiling (the first also carries the header), and the last part takes
    the remainder. That trades the balanced sizes for boundaries a restore
    can stream one part at a time."""
    chunk = payload_ceiling(max_size)

    if align_unit:
        per_part = (chunk - align_head) // align_unit
        if per_part < 1:
            raise ValueError(f"max_size ({max_size}) is too small for encrypted chunks of {align_unit} bytes")
        sizes = []
        remaining = size_bytes
        cap = align_head + per_part * align_unit
        while remaining > 0:
            take = min(cap, remaining)
            sizes.append(take)
            remaining -= take
            cap = per_part * align_unit
        assert sum(sizes) == size_bytes
        return sizes

    # Integer arithmetic throughout, not size_bytes / chunk: float division
    # loses precision for sizes approaching 2**53, and a silently-wrong part
    # count here corrupts every offset downstream. -(-a // b) is the
    # standard ceiling-division-via-floor-division trick.
    k = -(-size_bytes // chunk)
    q = -(-size_bytes // k)  # balanced part size (see note below)

    # Section 3 says "parts of equal size, except that the last part may be
    # smaller," which admits two readings: exactly `chunk` each with a
    # possibly-tiny remainder, or balanced at ceil(size / k). We use
    # balanced: it's what makes k meaningful (chunk-sized parts with a
    # leftover would make the "last part may be smaller" note describe a
    # near-empty final part whenever size == k * chunk + 1), and it avoids
    # exactly that 1-byte final part in that case.
    sizes = [q] * (k - 1) + [size_bytes - q * (k - 1)]

    assert q <= chunk
    assert all(s > 0 for s in sizes)
    assert sum(sizes) == size_bytes

    return sizes


def _roundup(n: int, multiple: int) -> int:
    return -(-n // multiple) * multiple


def _member_cost(size_bytes: int) -> int:
    """Conservative estimate of one clump member's contribution to the tar's
    size, used only to decide when to seal a clump before it would exceed
    clump_size. 512-byte header + data rounded up to a 512-byte block, plus
    1024 bytes of headroom for a pax extended header (measured fact 3) in
    case the member's name is long. The real, exact size is measured after
    sealing when offsets are read back - this is deliberately an
    overestimate, not the final answer."""
    return 512 + _roundup(size_bytes, 512) + 1024


def _write_tar(
    dest_dir: Path,
    archive_id: str,
    entries: list[tuple[str, Path, int, int, int]],
) -> tuple[Path, dict[str, int]]:
    """Write one sealed tar containing `entries` (member_name, source_path,
    seek_offset, size, mtime_ns each), then reopen it to read back each
    member's real offset_data.

    addfile() does copy.copy(ti) internally (measured fact 1), so the
    TarInfo objects built here never receive an offset - hand-computing one
    would be a guess, not a fact. We always reopen the sealed tar afterward
    and take offset_data from getmembers(), keyed by member name.
    """
    local_path = dest_dir / f"{archive_id}.tar"
    with tarfile.open(local_path, "w", format=tarfile.PAX_FORMAT) as tf:
        for member_name, source_path, seek_offset, size, mtime_ns in entries:
            ti = tarfile.TarInfo(name=member_name)
            ti.size = size
            ti.mtime = mtime_ns // 1_000_000_000
            # mode/uid/gid/uname/gname are left at TarInfo's defaults
            # (0o644/0/0/""/"") rather than copied from the source file's
            # stat, so packing the same input twice produces byte-identical
            # tar contents regardless of the source file's real permissions.
            with source_path.open("rb") as f:
                f.seek(seek_offset)
                # tarfile reads exactly ti.size bytes from f's current
                # position, so we hand it the open file object directly
                # instead of a read() slice - a multi-GB file is never held
                # in memory here, only streamed straight through to disk.
                tf.addfile(ti, f)

    offsets: dict[str, int] = {}
    with tarfile.open(local_path) as tf:
        for member in tf.getmembers():
            offsets[member.name] = member.offset_data

    return local_path, offsets


def _build_clump(contents: list[_UniqueContent], dest_dir: Path) -> PackedArchive:
    archive_id = str(uuid.uuid4())
    entries = [(c.member_name, c.source_path, 0, c.size_bytes, c.mtime_ns) for c in contents]
    local_path, offsets = _write_tar(dest_dir, archive_id, entries)
    members = [
        PackedMember(
            sha256=c.sha256,
            content_id=c.content_id,
            paths=c.paths,
            size_bytes=c.size_bytes,
            mtime_ns=c.mtime_ns,
            member_name=c.member_name,
            offset=offsets[c.member_name],
            part=None,
            part_count=None,
            compression=c.compression,
        )
        for c in contents
    ]
    return PackedArchive(
        archive_id=archive_id,
        archive_type=CLUMP,
        local_path=local_path,
        size_bytes=local_path.stat().st_size,
        members=members,
    )


def _build_single(content: _UniqueContent, dest_dir: Path) -> PackedArchive:
    archive_id = str(uuid.uuid4())
    entries = [(content.member_name, content.source_path, 0, content.size_bytes, content.mtime_ns)]
    local_path, offsets = _write_tar(dest_dir, archive_id, entries)
    member = PackedMember(
        sha256=content.sha256,
        content_id=content.content_id,
        paths=content.paths,
        size_bytes=content.size_bytes,
        mtime_ns=content.mtime_ns,
        member_name=content.member_name,
        offset=offsets[content.member_name],
        part=None,
        part_count=None,
        compression=content.compression,
    )
    return PackedArchive(
        archive_id=archive_id,
        archive_type=SINGLE,
        local_path=local_path,
        size_bytes=local_path.stat().st_size,
        members=[member],
    )


def _build_parts(
    content: _UniqueContent, dest_dir: Path, max_size: int, align_unit: int = 0, align_head: int = 0
) -> list[PackedArchive]:
    part_sizes = split_part_sizes(
        content.size_bytes, max_size=max_size, align_unit=align_unit, align_head=align_head
    )
    part_count = len(part_sizes)
    archives = []
    seek_offset = 0
    for i, part_size in enumerate(part_sizes, start=1):
        archive_id = str(uuid.uuid4())
        member_name = f"{content.member_name}.part{i:04d}"
        entries = [(member_name, content.source_path, seek_offset, part_size, content.mtime_ns)]
        local_path, offsets = _write_tar(dest_dir, archive_id, entries)
        member = PackedMember(
            sha256=content.sha256,
            content_id=content.content_id,
            paths=content.paths,
            size_bytes=content.size_bytes,  # whole file's size, per spec section 5
            mtime_ns=content.mtime_ns,
            member_name=member_name,
            offset=offsets[member_name],
            part=i,
            part_count=part_count,
            compression=content.compression,
        )
        archives.append(
            PackedArchive(
                archive_id=archive_id,
                archive_type=PART,
                local_path=local_path,
                size_bytes=local_path.stat().st_size,
                members=[member],
            )
        )
        seek_offset += part_size
    return archives


def pack(
    files: Sequence[FileToPack],
    *,
    dest_dir: Path,
    min_size: int,
    clump_size: int,
    max_size: int,
    align_unit: int = 0,
    align_head: int = 0,
) -> list[PackedArchive]:
    """Pack `files` into archives per spec section 3. `dest_dir` must already
    exist; pack() does not create or clean it up (step 5 owns that temp
    directory's lifecycle). Does not mutate `files`."""

    # 1. Deduplicate by sha256 (spec section 5).
    groups: dict[str, list[FileToPack]] = {}
    for f in files:
        groups.setdefault(f.sha256, []).append(f)

    unique_contents = []
    for sha256, group in groups.items():
        rep = min(group, key=lambda f: f.rel_path)
        paths = sorted(f.rel_path for f in group)
        unique_contents.append(
            _UniqueContent(
                sha256=sha256,
                content_id=rep.content_id,
                rel_path=rep.rel_path,
                paths=paths,
                source_path=rep.source_path,
                size_bytes=rep.size_bytes,
                mtime_ns=rep.mtime_ns,
                member_stem=rep.member_stem,
                compression=rep.compression,
            )
        )

    # 2. Sort the unique contents by the representative's rel_path (spec
    # section 3: "added in path order", so a directory's files tend to land
    # in the same clump). This is a new list, so the caller's input sequence
    # is never touched.
    unique_contents.sort(key=lambda c: c.rel_path)

    # A clump is sealed at clump_size, but never above the payload ceiling:
    # step 01 allows clump_size == max_size, and at that setting a clump's
    # end-of-archive blocks and record padding would push the tar past
    # max_size, violating spec section 9 the same way an unmargined single
    # archive would.
    clump_ceiling = min(clump_size, payload_ceiling(max_size))

    archives: list[PackedArchive] = []
    open_clump: list[_UniqueContent] = []
    open_clump_cost = 0

    def seal_open_clump() -> None:
        nonlocal open_clump, open_clump_cost
        if open_clump:
            archives.append(_build_clump(open_clump, dest_dir))
        open_clump = []
        open_clump_cost = 0

    # 3. Walk in path order, dispatching on classify. The open clump spans
    # the whole walk (not just consecutive clump-eligible files): a
    # single/part file encountered in between does not force the clump
    # closed, it just gets its own archive alongside it.
    for content in unique_contents:
        kind = classify(content.size_bytes, min_size=min_size, max_size=max_size)
        if kind == CLUMP:
            cost = _member_cost(content.size_bytes)
            if open_clump and open_clump_cost + cost > clump_ceiling:
                seal_open_clump()
            # step 01 validates min_size < clump_size, so a single
            # clump-eligible file (size_bytes < min_size) can never alone
            # exceed clump_size - assert the invariant rather than adding a
            # fallback path that v2.0 has no use for.
            assert cost <= clump_ceiling, (
                f"clump-eligible file of {content.size_bytes} bytes alone exceeds "
                f"the clump ceiling ({clump_ceiling}); check min_size < clump_size "
                f"and min_size < max_size - 1 MiB"
            )
            open_clump.append(content)
            open_clump_cost += cost
        elif kind == SINGLE:
            archives.append(_build_single(content, dest_dir))
        else:
            archives.extend(_build_parts(content, dest_dir, max_size, align_unit, align_head))

    # Leftover small files at the end of the run go into a final, smaller
    # clump (spec section 3's last bullet).
    seal_open_clump()

    return archives
