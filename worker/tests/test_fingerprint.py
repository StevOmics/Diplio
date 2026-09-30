from pathlib import Path

from app.fingerprint import compute_fingerprint

# Kept identical to web/tests/test_fingerprint.py on purpose - see the comment
# there. Verify (worker) checks a backup file's fingerprint against the value
# web computed at scan time, so both copies of this algorithm must agree.
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


def test_large_file_only_samples_head_and_tail(tmp_path: Path):
    f = tmp_path / "large.bin"
    size = 3 * 1024 * 1024
    data = bytearray(size)
    f.write_bytes(bytes(data))
    original = compute_fingerprint(f, f.stat().st_size)

    data[size // 2] = 0xFF
    f.write_bytes(bytes(data))
    assert compute_fingerprint(f, f.stat().st_size) == original
