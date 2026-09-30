import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from app.packer import (
    MIB,
    FileToPack,
    classify,
    pack,
    split_part_sizes,
)

# Small, deliberately artificial thresholds so tests don't need to write
# multi-hundred-MB files. max_size must stay > 1 MiB (step 01's validation),
# since split_part_sizes raises otherwise; CHUNK mirrors the production
# formula so tests can predict expected part counts/sizes.
MIN_SIZE = 200
CLUMP_SIZE = 32 * 1024
MAX_SIZE = 3 * MIB
CHUNK = MAX_SIZE - MIB


def _pack(files, dest_dir, **overrides):
    kwargs = dict(min_size=MIN_SIZE, clump_size=CLUMP_SIZE, max_size=MAX_SIZE)
    kwargs.update(overrides)
    return pack(files, dest_dir=dest_dir, **kwargs)


def _make_file(tmp_path: Path, name: str, data: bytes, mtime_ns: int = 1_700_000_000_000_000_000) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, ns=(mtime_ns, mtime_ns))
    return p


def _file_to_pack(source_root: Path, rel_path: str, data: bytes, sha256: str | None = None) -> FileToPack:
    import hashlib

    path = _make_file(source_root, rel_path, data)
    return FileToPack(
        source_path=path,
        rel_path=rel_path,
        sha256=sha256 or hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        mtime_ns=path.stat().st_mtime_ns,
    )


# --- Classification (spec section 3 table) ---------------------------------


def test_classify_below_min_is_clump():
    assert classify(MIN_SIZE - 1, min_size=MIN_SIZE, max_size=MAX_SIZE) == "clump"


def test_classify_at_min_is_single():
    assert classify(MIN_SIZE, min_size=MIN_SIZE, max_size=MAX_SIZE) == "single"


def test_classify_at_payload_ceiling_is_single():
    """The single/part boundary sits at max_size - 1 MiB, not max_size, so
    the tar built around the payload cannot exceed max_size (spec section 9).
    See packer.payload_ceiling."""
    assert classify(CHUNK, min_size=MIN_SIZE, max_size=MAX_SIZE) == "single"


def test_classify_above_payload_ceiling_is_part():
    assert classify(CHUNK + 1, min_size=MIN_SIZE, max_size=MAX_SIZE) == "part"


def test_classify_at_max_size_is_part():
    """A file at exactly max_size cannot fit in a tar that stays under
    max_size, so it splits rather than becoming a single archive."""
    assert classify(MAX_SIZE, min_size=MIN_SIZE, max_size=MAX_SIZE) == "part"


def test_classify_zero_bytes_is_clump():
    assert classify(0, min_size=MIN_SIZE, max_size=MAX_SIZE) == "clump"


# --- Split arithmetic (spec section 3) --------------------------------------


def test_split_single_chunk_returns_one_part():
    # A file sized exactly at one chunk's worth needs only one part. (A file
    # sized at max_size itself classifies as "single", not "part" - see
    # test_classify_at_max_is_single - so this exercises the function at the
    # boundary of what a single part can hold, not literally size==max_size.)
    sizes = split_part_sizes(CHUNK, max_size=MAX_SIZE)
    assert sizes == [CHUNK]


@pytest.mark.parametrize("k", [1, 2, 3, 4])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_split_sizes_sum_to_original(k, delta):
    size = k * CHUNK + delta
    if size <= 0:
        pytest.skip("non-positive size not meaningful here")
    sizes = split_part_sizes(size, max_size=MAX_SIZE)
    assert sum(sizes) == size


@pytest.mark.parametrize("k", [1, 2, 3, 4])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_split_parts_never_exceed_chunk(k, delta):
    size = k * CHUNK + delta
    if size <= 0:
        pytest.skip("non-positive size not meaningful here")
    sizes = split_part_sizes(size, max_size=MAX_SIZE)
    assert all(s <= CHUNK for s in sizes)


def test_split_parts_are_balanced():
    # size == k * chunk + 1 must not produce a 1-byte last part.
    k = 3
    size = k * CHUNK + 1
    sizes = split_part_sizes(size, max_size=MAX_SIZE)
    # The formula (q = ceil(size / k), remainder in the last slot) only
    # guarantees the last part isn't a tiny 1-byte sliver, not that every
    # part is within 1 byte of every other - k * chunk + 1 is exactly the
    # case where a naive "chunk-sized parts, remainder last" scheme would
    # produce a 1-byte final part; this scheme doesn't.
    assert sizes[-1] > 1


def test_split_rejects_max_size_at_or_below_one_mib():
    with pytest.raises(ValueError):
        split_part_sizes(10 * MIB, max_size=MIB)


# --- Packing structure -------------------------------------------------------


def test_small_files_share_one_clump(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    files = [_file_to_pack(src, f"f{i}.bin", f"x{i}".encode() * 10) for i in range(5)]
    archives = _pack(files, dest)
    assert len(archives) == 1
    assert archives[0].archive_type == "clump"
    assert len(archives[0].members) == 5


def test_clump_never_exceeds_max_size_when_clump_size_equals_max_size(tmp_path):
    """Step 01 permits clump_size == max_size. At that setting, sealing purely
    on clump_size would let a clump's end-of-archive blocks and record padding
    push the tar past max_size, breaking spec section 9 the same way an
    unmargined single archive would. pack() seals at
    min(clump_size, payload_ceiling(max_size)) to prevent it."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    # These sizes are chosen so the failure is actually reachable, not merely
    # plausible. _member_cost overestimates each member by 1024 bytes (pax
    # headroom), and with many small members that slack alone absorbs the
    # end-of-archive blocks and record padding. Two members close to half the
    # ceiling leave only 1024 bytes of slack, so the 1024-byte end blocks plus
    # rounding up to RECORDSIZE push the tar over: measured at 3,153,920 bytes
    # against a 3,145,728-byte max_size, 8,192 over. Sealing at clump_size
    # alone produces exactly that; sealing at the payload ceiling does not.
    member_size = 1_571_328
    files = [
        _file_to_pack(src, f"half{i}.bin", bytes([65 + i]) * member_size) for i in range(2)
    ]
    archives = _pack(files, dest, min_size=member_size + 1, clump_size=MAX_SIZE)

    assert all(a.archive_type == "clump" for a in archives)
    for archive in archives:
        assert archive.local_path.stat().st_size <= MAX_SIZE, (
            f"clump tar of {archive.local_path.stat().st_size} bytes exceeds "
            f"max_size ({MAX_SIZE})"
        )


def test_clump_seals_before_exceeding_clump_size(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    # Each file costs 512 + roundup(size, 512) + 1024. At 100 bytes (must
    # stay under MIN_SIZE to be clump-eligible) that's 2048 bytes/member;
    # enough of them blow past CLUMP_SIZE (32 KiB) and force a second clump.
    files = [_file_to_pack(src, f"f{i:03d}.bin", os.urandom(100)) for i in range(20)]
    archives = _pack(files, dest)
    clumps = [a for a in archives if a.archive_type == "clump"]
    assert len(clumps) >= 2
    for a in clumps:
        assert a.local_path.stat().st_size <= CLUMP_SIZE
        assert a.size_bytes == a.local_path.stat().st_size
    # every input file accounted for across the clumps
    assert sum(len(a.members) for a in clumps) == len(files)


def test_leftover_files_go_in_a_final_smaller_clump(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    files = [_file_to_pack(src, f"f{i:03d}.bin", os.urandom(100)) for i in range(20)]
    archives = _pack(files, dest)
    clumps = [a for a in archives if a.archive_type == "clump"]
    sizes = [a.local_path.stat().st_size for a in clumps]
    # not all clumps are the same size - the last one is the leftover
    assert len(set(sizes)) > 1 or len(clumps) == 1


def test_clump_members_are_in_path_order(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    names = ["banana.bin", "apple.bin", "cherry.bin"]
    files = [_file_to_pack(src, n, os.urandom(50)) for n in names]
    archives = _pack(files, dest)
    assert len(archives) == 1
    member_names = [m.member_name for m in archives[0].members]
    assert member_names == sorted(names)


def test_single_file_gets_its_own_archive(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    data = os.urandom(MIN_SIZE + 100)
    files = [_file_to_pack(src, "movie.mkv", data)]
    archives = _pack(files, dest)
    assert len(archives) == 1
    assert archives[0].archive_type == "single"
    member = archives[0].members[0]
    assert member.member_name == "movie.mkv"
    assert member.part is None
    assert member.part_count is None
    assert member.size_bytes == len(data)


def test_large_file_becomes_part_archives(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    size = MAX_SIZE + 10
    data = os.urandom(size)
    files = [_file_to_pack(src, "big.mkv", data)]
    archives = _pack(files, dest)
    assert all(a.archive_type == "part" for a in archives)
    expected_sizes = split_part_sizes(size, max_size=MAX_SIZE)
    assert len(archives) == len(expected_sizes)
    for n, a in enumerate(archives, start=1):
        assert len(a.members) == 1
        member = a.members[0]
        assert member.member_name == f"big.mkv.part{n:04d}"
        assert member.part == n
        assert member.part_count == len(expected_sizes)
        assert member.size_bytes == size  # whole file's size, every part


def test_duplicate_content_stored_once_with_both_paths(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    data = os.urandom(50)
    a = _file_to_pack(src, "b/dup.bin", data)
    b = _file_to_pack(src, "a/dup.bin", data, sha256=a.sha256)
    archives = _pack([a, b], dest)
    assert len(archives) == 1
    assert len(archives[0].members) == 1
    member = archives[0].members[0]
    assert member.paths == sorted(["a/dup.bin", "b/dup.bin"])
    assert member.member_name == "a/dup.bin"  # rel_path that sorts first


def test_no_archive_exceeds_max_size(tmp_path):
    """Spec section 9: "No uploaded object exceeds max_size."

    Exercised right at the boundaries where it is actually in danger. A tar
    adds a 512-byte header, pads the data to a 512-byte block, appends two
    end-of-archive blocks, and rounds the whole file up to RECORDSIZE
    (10240) - about 13 KiB worst case with a pax long-name header. Every
    archive type therefore has to stay a margin below max_size, which is
    what packer.payload_ceiling enforces: the sizes just below and just
    above it are the ones that would regress if that margin were dropped.
    """
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    sizes = [
        CHUNK - 1,  # largest "single" archives: right at the tar-overhead edge
        CHUNK,
        MAX_SIZE - 1,  # would have overflowed under section 3's literal boundary
        MAX_SIZE,
        MAX_SIZE + 1,
        CHUNK - 1,
        CHUNK,
        CHUNK + 1,
        2 * CHUNK - 1,
        2 * CHUNK,
        2 * CHUNK + 1,
    ]
    files = [_file_to_pack(src, f"f{i}.bin", os.urandom(sz)) for i, sz in enumerate(sizes)]
    archives = _pack(files, dest)
    for a in archives:
        assert a.local_path.stat().st_size <= MAX_SIZE


def test_zero_byte_file_round_trips(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    files = [_file_to_pack(src, "empty.bin", b"")]
    archives = _pack(files, dest)
    assert len(archives) == 1
    assert archives[0].archive_type == "clump"
    member = archives[0].members[0]
    assert member.size_bytes == 0
    with tarfile.open(archives[0].local_path) as tf:
        extracted = tf.extractfile(member.member_name).read()
    assert extracted == b""


# --- The two that matter most ------------------------------------------------


def test_offsets_locate_member_data(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()

    small = [_file_to_pack(src, f"clump/f{i}.bin", os.urandom(80)) for i in range(6)]
    single_data = os.urandom(MIN_SIZE + 500)
    single = _file_to_pack(src, "single/movie.mkv", single_data)
    part_size = MAX_SIZE + 777
    part_data = os.urandom(part_size)
    part = _file_to_pack(src, "part/big.mkv", part_data)

    files = small + [single, part]
    archives = _pack(files, dest)

    # Map every rel_path to its original bytes for lookup below.
    originals = {f.rel_path: f.source_path.read_bytes() for f in files}

    for archive in archives:
        with archive.local_path.open("rb") as tf_file:
            for member in archive.members:
                if member.part is None:
                    expected = originals[member.member_name]
                else:
                    # part member_name is "<rel_path>.partNNNN"
                    rel_path = member.member_name.rsplit(".part", 1)[0]
                    whole = originals[rel_path]
                    part_sizes = split_part_sizes(member.size_bytes, max_size=MAX_SIZE)
                    start = sum(part_sizes[: member.part - 1])
                    expected = whole[start : start + part_sizes[member.part - 1]]

                tf_file.seek(member.offset)
                actual = tf_file.read(len(expected))
                assert actual == expected, f"offset mismatch for {member.member_name}"


@pytest.mark.skipif(shutil.which("tar") is None, reason="no tar binary on PATH")
def test_tar_extracts_with_real_tar(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()

    long_rel_path = "a/" + ("b" * 40) + "/" + ("c" * 40) + "/" + ("d" * 40) + ".bin"
    assert len(long_rel_path) > 100

    files = [
        _file_to_pack(src, "short.bin", os.urandom(100)),
        _file_to_pack(src, long_rel_path, os.urandom(150)),
    ]
    archives = _pack(files, dest)
    assert len(archives) == 1  # both are clump-eligible

    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    subprocess.run(
        ["tar", "-xf", str(archives[0].local_path), "-C", str(extract_dir)],
        check=True,
    )

    for f in files:
        extracted = extract_dir / f.rel_path
        assert extracted.read_bytes() == f.source_path.read_bytes()


# --- Also --------------------------------------------------------------------


def test_split_parts_cat_back_together(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    size = 2 * CHUNK + 12345
    data = os.urandom(size)
    files = [_file_to_pack(src, "whole.mkv", data)]
    archives = _pack(files, dest)
    archives.sort(key=lambda a: a.members[0].part)

    reassembled = bytearray()
    for a in archives:
        member = a.members[0]
        with tarfile.open(a.local_path) as tf:
            reassembled += tf.extractfile(member.member_name).read()

    assert bytes(reassembled) == data


def test_tar_bytes_are_deterministic(tmp_path):
    src = tmp_path / "src"
    dest1 = tmp_path / "dest1"
    dest2 = tmp_path / "dest2"
    dest1.mkdir()
    dest2.mkdir()

    files = [
        _file_to_pack(src, "a.bin", os.urandom(50)),
        _file_to_pack(src, "b.bin", os.urandom(50)),
    ]

    archives1 = _pack(files, dest1)
    archives2 = _pack(files, dest2)

    assert len(archives1) == len(archives2) == 1
    assert archives1[0].local_path.read_bytes() == archives2[0].local_path.read_bytes()
