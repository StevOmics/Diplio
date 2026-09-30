#!/usr/bin/env python3
"""Generate a synthetic file set for exercising MediaBridge's v2 backup pipeline.

The set is sized to hit every archive type at the *test* thresholds below
(min 1 MiB, clump 64 MiB, max 64 MiB - the smallest max the Settings page
allows), so a real run produces clumps, single archives, and split parts:

  tiny  (< 1 MiB)           -> clump archives   (incl. one 0-byte file)
  mid   (1 MiB .. ~63 MiB)  -> single archives
  large (> ~63 MiB)         -> part archives    (split, then reassembled on restore)

It also writes files with duplicate content (same hash, different paths - the
"stored once, two paths" case) and names with spaces / unicode / deep folders.

Content is pseudo-random from a fixed seed, so re-running produces identical
files (same hashes) - a second backup run should therefore skip everything as
"unchanged". Use --seed to get a different set.

Usage:
  scripts/make-test-media.py                    # -> $FILESYSTEM_ROOT (or ./mnt_ro)/mediabridge-test-set
  scripts/make-test-media.py --out /some/dir --large-mib 150
  scripts/make-test-media.py --clean            # delete the generated set

Then: add that folder as a storage location (media type "Files"), scan it,
set Settings > Backup archiving to min 1 MiB / max 64 MiB, and use the
Libraries page's "Backup to Cloud Archive".
"""
import argparse
import hashlib
import os
import random
import shutil
import sys
from pathlib import Path

MIB = 1024 * 1024
MARKER = ".mediabridge-test-set"


def default_out() -> Path:
    root = os.environ.get("FILESYSTEM_ROOT") or "./mnt_ro"
    return Path(root) / "mediabridge-test-set"


def write_random(path: Path, size: int, rng: random.Random) -> str:
    """Writes `size` pseudo-random bytes (streamed, so multi-hundred-MiB files
    don't sit in memory) and returns the SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    remaining = size
    with path.open("wb") as f:
        while remaining > 0:
            n = min(remaining, MIB)
            block = rng.randbytes(n)
            f.write(block)
            h.update(block)
            remaining -= n
    return h.hexdigest()


def build(out: Path, seed: int, mid_count: int, large_mib: list[int], tiny_count: int) -> list[tuple[str, int, str]]:
    rng = random.Random(seed)
    made: list[tuple[str, int, str]] = []

    def add(rel: str, size: int) -> None:
        digest = write_random(out / rel, size, rng)
        made.append((rel, size, digest))

    # tiny: clump candidates, spread across folders
    kinds = [("photos", "jpg"), ("docs", "txt"), ("music/album", "mp3"), ("docs/notes", "md")]
    for i in range(tiny_count):
        folder, ext = kinds[i % len(kinds)]
        add(f"{folder}/tiny-{i:03d}.{ext}", rng.randint(1_000, 300 * 1024))
    add("docs/empty.txt", 0)

    # mid: single archives
    for i in range(mid_count):
        add(f"movies/mid-{i:02d}.mkv", rng.randint(2 * MIB, 20 * MIB))

    # large: split into parts
    for i, mib in enumerate(large_mib):
        add(f"movies/large-{i:02d}.mp4", mib * MIB + rng.randint(0, 999))

    # awkward names
    add("Holiday Photos 2024/IMG 001 (final).jpg", 150 * 1024)
    add("music/Björk – Jóga.mp3", 400 * 1024)
    add("deep/a/b/c/d/e/f/deeply nested file.txt", 5_000)

    # duplicate content under different paths: identical bytes, same hash
    for src, dup in [("photos/tiny-000.jpg", "photos/copy of tiny-000.jpg"), ("movies/mid-00.mkv", "movies/mid-00 (copy).mkv")]:
        if (out / src).exists():
            shutil.copyfile(out / src, out / dup)
            made.append((dup, (out / src).stat().st_size, next(d for r, _, d in made if r == src)))

    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=None, help="output folder (default: $FILESYSTEM_ROOT or ./mnt_ro, then /mediabridge-test-set)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tiny", type=int, default=40, help="number of tiny (clump) files")
    ap.add_argument("--mid", type=int, default=6, help="number of mid (single-archive) files")
    ap.add_argument("--large-mib", type=int, nargs="*", default=[90, 140], help="sizes of large (split) files in MiB; pass none to skip")
    ap.add_argument("--clean", action="store_true", help="delete a previously generated set and exit")
    args = ap.parse_args()

    out = (args.out or default_out()).resolve()

    if args.clean:
        if not (out / MARKER).exists():
            print(f"refusing to delete {out}: no {MARKER} marker (not a generated test set)", file=sys.stderr)
            return 1
        shutil.rmtree(out)
        print(f"removed {out}")
        return 0

    if out.exists() and any(out.iterdir()) and not (out / MARKER).exists():
        print(f"refusing to write into {out}: it is not empty and not a generated test set", file=sys.stderr)
        return 1

    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("generated by scripts/make-test-media.py\n")
    made = build(out, args.seed, args.mid, args.large_mib, args.tiny)

    total = sum(size for _, size, _ in made)
    print(f"wrote {len(made)} files, {total / MIB:.1f} MiB to {out}")
    print("  clump candidates (<1 MiB):", sum(1 for _, s, _ in made if s < MIB))
    print("  single (1-63 MiB):        ", sum(1 for _, s, _ in made if MIB <= s <= 63 * MIB))
    print("  split  (>63 MiB):         ", sum(1 for _, s, _ in made if s > 63 * MIB))
    print("  duplicate-content files:  ", len(made) - len({d for _, _, d in made}))
    print("\nNext: add this folder as a 'Files' storage location, scan, set Settings > Backup archiving")
    print("to min 1 MiB / max 64 MiB, then Libraries > Backup to Cloud Archive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
