from pathlib import Path

from app.fingerprint import compute_fingerprint

# This exact digest is duplicated in worker/tests/test_fingerprint.py on purpose:
# web and worker each carry their own copy of the fingerprint algorithm (worker
# can't import web's code across the service boundary), and a fingerprint
# computed by one has to be comparable to one computed by the other (that's how
# Verify checks a backup against the catalog). If either copy's algorithm ever
# drifts, this shared known-answer test fails in whichever service changed.
KNOWN_SMALL_FINGERPRINT = "69c25ed8ca74ac3f0ae511e78e26bbcf7fdd341408122f00a164ed1cc4ea5c21"
KNOWN_EMPTY_FINGERPRINT = "0fd923ca5e7218c4ba3c3801c26a617ecdbfdaebb9c76ce2eca166e7855efbb8"


def test_known_answer_small_file(tmp_path: Path):
    f = tmp_path / "small.bin"
    f.write_bytes(b"hello world!")
    assert compute_fingerprint(f, f.stat().st_size) == KNOWN_SMALL_FINGERPRINT


def test_known_answer_empty_file(tmp_path: Path):
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    assert compute_fingerprint(f, f.stat().st_size) == KNOWN_EMPTY_FINGERPRINT


def test_deterministic(tmp_path: Path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 5000)
    assert compute_fingerprint(f, f.stat().st_size) == compute_fingerprint(f, f.stat().st_size)


def test_different_content_different_fingerprint(tmp_path: Path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"content A")
    b.write_bytes(b"content B")
    assert compute_fingerprint(a, a.stat().st_size) != compute_fingerprint(b, b.stat().st_size)


def test_same_content_same_fingerprint_different_paths(tmp_path: Path):
    a = tmp_path / "a.bin"
    b = tmp_path / "sub" / "b.bin"
    b.parent.mkdir()
    a.write_bytes(b"identical bytes")
    b.write_bytes(b"identical bytes")
    assert compute_fingerprint(a, a.stat().st_size) == compute_fingerprint(b, b.stat().st_size)


def test_large_file_only_samples_head_and_tail(tmp_path: Path):
    # A file larger than 2x the sample size should fingerprint using only the
    # head/tail, so changing an untouched middle byte must not change the result.
    f = tmp_path / "large.bin"
    size = 3 * 1024 * 1024
    data = bytearray(size)
    f.write_bytes(bytes(data))
    original = compute_fingerprint(f, f.stat().st_size)

    data[size // 2] = 0xFF  # flip a byte in the middle, outside the sampled regions
    f.write_bytes(bytes(data))
    assert compute_fingerprint(f, f.stat().st_size) == original


def test_large_file_head_change_changes_fingerprint(tmp_path: Path):
    f = tmp_path / "large.bin"
    size = 3 * 1024 * 1024
    data = bytearray(size)
    f.write_bytes(bytes(data))
    original = compute_fingerprint(f, f.stat().st_size)

    data[0] = 0xFF  # first byte, inside the sampled head region
    f.write_bytes(bytes(data))
    assert compute_fingerprint(f, f.stat().st_size) != original


def test_size_is_part_of_fingerprint(tmp_path: Path):
    # Two files with identical head/tail bytes but different sizes (e.g. a
    # truncated copy) must not collide, since size is hashed in first.
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"same prefix content")
    b.write_bytes(b"same prefix content" + b"extra")
    assert compute_fingerprint(a, a.stat().st_size) != compute_fingerprint(b, b.stat().st_size)
