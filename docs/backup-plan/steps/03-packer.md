# Step 03: Packer — classify, clump, split, build tars

- **Phase:** step 3 of 8 (see `docs/backup-plan/PLAN.md`)
- **Spec sections to read:** `docs/cfa-spec.md` §3 (Packing), §2 (the size settings), §5 (why `offset` exists), §9 (the tests this step must satisfy)
- **Depends on:** 01 (the size settings), 02 (`HashedFile`)

## Goal
Pure functions that classify files by size, group small ones into clump tars, split oversized ones into part tars, write every archive to a temp directory, and report each member's byte offset within its tar. **Nothing calls them yet** — step 5 is the first consumer.

## Context
- Spec §3 is the whole specification for this step. Read it before anything else. The three archive types are `clump` (many small files), `single` (one file), `part` (one slice of one large file).
- §5 stores a byte `offset` per member so a single file can be fetched with a ranged read, and §7 restores that way. **Those offsets are what makes step 6 possible**; a wrong offset is a bug that only surfaces when someone tries to restore.
- Spec §3 mandates plain uncompressed tar. Compression is not merely unnecessary here (media files are already compressed) — it would make ranged reads at a byte offset impossible, since you cannot seek to a member boundary in a compressed stream.
- The three sizes arrive as arguments, not from the database. Step 01 put them on `TransferConfig` (`min_size_bytes`, `clump_size_bytes`, `max_size_bytes`) and validated `min < clump <= max` and `max > 1 MiB`; step 5 reads them and passes them in.
- Step 02 gives you `HashedFile` (`sha256`, `size_bytes`, `mtime_ns`) in `worker/app/fingerprint.py`. This step takes those values as inputs; it does not hash anything.

### Three measured facts about Python's `tarfile` that dictate the implementation

These were verified in this repo's environment. Do not re-derive them differently.

1. **`tf.addfile(ti, f)` does `copy.copy(ti)` internally**, so the `TarInfo` you pass in never receives an offset — its `offset_data` stays `0`. Offsets **must** be read back by reopening the sealed tar and taking `offset_data` from `getmembers()`, keyed by member name. Never hand-compute an offset, and never read it from the object you passed to `addfile`.
2. **ustar caps member names at 100 characters**, and real media paths exceed that routinely. Python's `PAX_FORMAT` handles them and `bsdtar 3.5.3` extracts them correctly. Pass `format=tarfile.PAX_FORMAT` explicitly rather than relying on the default.
3. **A tar file is padded to `tarfile.RECORDSIZE` (10240 bytes).** Total overhead is 512 (header) + up to 511 (data padding) + 1024 (two end-of-archive blocks) + up to 10239 (record padding), plus roughly 1024 more for a pax long-name header: about 13 KiB worst case. The spec's 1 MiB split margin covers that comfortably.

## Changes

### `worker/app/packer.py` (new, ~200 lines)
Worker-only — packing happens in the worker. Do **not** create a `web/` mirror; the web service never packs.

No imports from `app.models`, `app.db`, `app.gcs`, or `app.config`. This module is pure logic over its arguments so it stays in the dependency-free suite.

```python
MIB = 1024 * 1024

@dataclass(frozen=True)
class FileToPack:
    source_path: Path      # absolute path to read bytes from
    rel_path: str          # path relative to its source root; the tar member name
    sha256: str
    size_bytes: int
    mtime_ns: int

@dataclass(frozen=True)
class PackedMember:
    sha256: str
    paths: list[str]       # more than one when files in the run share a hash
    size_bytes: int        # the whole file's size, even for a part
    mtime_ns: int
    member_name: str
    offset: int            # byte offset of this member's data within the tar
    part: int | None       # 1-based; None for clump and single
    part_count: int | None # None for clump and single

@dataclass(frozen=True)
class PackedArchive:
    archive_id: str        # uuid4, str(uuid.uuid4())
    archive_type: str      # "clump" | "single" | "part"
    local_path: Path       # dest_dir / f"{archive_id}.tar"
    size_bytes: int        # local_path.stat().st_size
    members: list[PackedMember]

def classify(size_bytes: int, *, min_size: int, max_size: int) -> str
def split_part_sizes(size_bytes: int, *, max_size: int) -> list[int]
def pack(files: Sequence[FileToPack], *, dest_dir: Path,
         min_size: int, clump_size: int, max_size: int) -> list[PackedArchive]
```

**`classify`** — §3's table, with one correction. `size_bytes < min_size` → `"clump"`; `min_size <= size_bytes <= payload_ceiling(max_size)` → `"single"`; above that → `"part"`. A 0-byte file classifies as `"clump"`. `clump_size` is not a parameter: it governs sealing, not classification.

**Why not `max_size` literally:** §3's table puts the single/part boundary at `max_size`, but §9 requires that no uploaded object exceed `max_size`, and tar overhead applies regardless of archive type. Measured: a file one byte under a 3 MiB `max_size` yields a 3,153,920-byte tar, 8,192 over. So `payload_ceiling(max_size) = max_size - 1 MiB` — §3's own split margin — governs all three types: the single/part boundary, the split chunk size, and (with `clump_size`) the clump seal point. `pack` seals clumps at `min(clump_size, payload_ceiling(max_size))`, since step 01 permits `clump_size == max_size`.

**`split_part_sizes`** — implements §3's formula:
```
chunk = max_size - MIB          # the 1 MiB margin for tar overhead
k     = ceil(size_bytes / chunk)
q     = ceil(size_bytes / k)    # balanced parts
sizes = [q] * (k - 1) + [size_bytes - q * (k - 1)]
```
Raise `ValueError` if `chunk <= 0` (step 01's validation prevents it, but defend the boundary). Assert before returning: `q <= chunk`, every size `> 0`, and `sum(sizes) == size_bytes`.

§3's "parts of equal size, except that the last part may be smaller" admits two readings — exactly `chunk` with a possibly-tiny remainder, or balanced at `ceil(size/k)`. **Use balanced**, as written above: it is what makes `k` meaningful, and it avoids a 1-byte final part when `size == k * chunk + 1`. Put that reasoning in a comment.

**`pack`** — in this order:

1. **Deduplicate by `sha256`** (§5: "If two files in the same run share a hash, the content is stored once and `paths` lists both"). Group the input by hash. For each group the representative is the entry whose `rel_path` sorts first; `PackedMember.paths` is every `rel_path` in the group, sorted. Size and mtime come from the representative.
2. **Sort the unique contents by the representative's `rel_path`** (§3: "added in path order", so a directory's files tend to share a clump).
3. Walk them, dispatching on `classify`:
   - **clump** — accumulate into the open clump. Seal it *before* it would exceed `clump_size`: add while `running_cost + member_cost(size) <= clump_size`, otherwise seal and open a new one. `member_cost(size) = 512 + roundup(size, 512) + 1024`, the last term allowing for a pax long-name header. Leftovers at the end become a final, smaller clump (§3's last bullet). Member name is the `rel_path`.
   - **single** — one tar with one member, named `rel_path`.
   - **part** — `split_part_sizes`, then one tar per part, each with a single member named `f"{rel_path}.part{n:04d}"` with `n` 1-based (§3: "numbered from 0001"). Every part member records the **whole file's** `size_bytes`, with `part` and `part_count` set; `PackedMember.offset` is the offset within that part's own tar.
4. **Write each tar** to `dest_dir / f"{archive_id}.tar"` with `tarfile.open(..., "w", format=tarfile.PAX_FORMAT)`. On each `TarInfo` set only `size` and `mtime` (`mtime_ns // 1_000_000_000`), leaving mode/uid/gid/uname/gname at `TarInfo` defaults so output is deterministic.
5. **Stream part data** by `f.seek(offset)` on the source file and then `tf.addfile(ti, f)` — `tarfile` reads exactly `ti.size` bytes from the current position, so no wrapper class is needed and a multi-GB file is never held in memory. Add a comment saying that, or someone will later "fix" it into a `read()`.
6. **After sealing each tar**, reopen it with `tarfile.open(local_path)` and read `offset_data` from `getmembers()` to populate `PackedMember.offset`, keyed by member name. See measured fact 1 — this is not optional.

`dest_dir` must already exist; `pack` does not create or clean it (step 5 owns the temp directory's lifecycle).

## Do Not Touch
- `web/app/main.py:_plan_backup` and the percentage-based `TransferConfig` columns. The old planner keeps running until step 7 retires it.
- `worker/app/tasks.py`, `worker/app/encryption.py`, `worker/app/gcs.py`, `web/app/catalog.py`.
- `compute_fingerprint`, `sha256_file`, `hash_file` — this step consumes step 02's output, it does not hash.
- Any route, template, or model. This step adds no schema and no UI.

## Tests to Write First
`worker/tests/test_packer.py` (new, pure logic, `tmp_path` only).

Classification (§3 table):
- `test_classify_below_min_is_clump`, `test_classify_at_min_is_single`, `test_classify_at_max_is_single`, `test_classify_above_max_is_part`, `test_classify_zero_bytes_is_clump`

Split arithmetic (§3):
- `test_split_single_chunk_returns_one_part`: `size = chunk` (i.e. `max_size - 1 MiB`) → one part. Not `size = max_size`, which always yields `k = 2` since `chunk < max_size`.
- `test_split_sizes_sum_to_original` and `test_split_parts_never_exceed_chunk`: parametrized over sizes around `k * chunk ± 1` for k in 1..4
- `test_split_parts_are_balanced`: `size == k * chunk + 1` does not produce a 1-byte last part
- `test_split_rejects_max_size_at_or_below_one_mib`: `pytest.raises(ValueError)`

Packing structure:
- `test_small_files_share_one_clump`
- `test_clump_seals_before_exceeding_clump_size`: enough small files to force two clumps; assert each tar's size on disk is `<= clump_size`
- `test_leftover_files_go_in_a_final_smaller_clump`
- `test_clump_members_are_in_path_order`
- `test_single_file_gets_its_own_archive`
- `test_large_file_becomes_part_archives`: `part` and `part_count` set on every member, names `...part0001`, `...part0002`, whole-file `size_bytes` recorded
- `test_duplicate_content_stored_once_with_both_paths` (§5): two different `rel_path`s, identical bytes and hash → one member, `paths` has both, sorted
- `test_no_archive_exceeds_max_size` (§9): actual `stat().st_size` of every produced tar, at sizes straddling `max_size` and `k * chunk ± 1`
- `test_zero_byte_file_round_trips` (§9)

The two that matter most:
- `test_offsets_locate_member_data` — for **every** member of **every** archive: open the tar, `seek(offset)`, `read(member.size_bytes if part is None else part_size)`, and assert the bytes equal the corresponding slice of the source file. This is what protects step 6's ranged restore.
- `test_tar_extracts_with_real_tar` (§9) — `subprocess.run(["tar", "-xf", ...])` into a temp dir, then compare extracted bytes to the sources. **Include a member whose `rel_path` exceeds 100 characters** so the pax choice is covered by a test rather than only by measurement. Skip with `pytest.mark.skipif(shutil.which("tar") is None)`.

Also:
- `test_split_parts_cat_back_together` (§9): extract each part in order, concatenate, assert the result equals the original file byte-for-byte
- `test_tar_bytes_are_deterministic`: packing identical input twice produces byte-identical tar *contents* (the filename differs, since `archive_id` is a uuid4) — catches a stray `time.time()` or inherited file mode leaking into a header

## Commands
- Run: `cd worker && pytest -x -q tests/test_packer.py`
- Full suites: `cd worker && pytest -x -q` then `cd web && pytest -x -q`
- No Docker and no database are needed for this step.

## Done When
- [ ] All listed tests exist and pass
- [ ] Both full suites pass with no services running
- [ ] `worker/app/packer.py` imports nothing from `app.models`, `app.db`, `app.gcs`, or `app.config`
- [ ] `git diff` shows no change to `_plan_backup`, any worker task, or any model
- [ ] A row added at the top of `docs/CHANGES.md` (`260917 | ...`)

## Notes for the Implementer
- **Read spec §3 first.** It is about fifteen lines and it is the entire specification for this step.
- The offset test is the reason this step exists. If you are short on time, cut a classification test, never that one.
- Use `math.ceil` on integers via `-(-a // b)` or `math.ceil(a / b)` carefully: for sizes near 2^53 float division loses precision. Prefer `-(-size // chunk)` integer arithmetic and say why in a comment.
- `roundup(n, 512)` is `-(-n // 512) * 512`.
- Do not add a member-count cap to clumps. v2.0 has no `clump_max_files`; only `clump_size` seals a clump.
- A single clump-eligible file can never exceed `clump_size` on its own, because step 01 validates `min_size < clump_size`. You may assert it, but do not add a fallback path for it.
- `pack` must not mutate its input sequence (no in-place `.sort()` on the caller's list).
- Keep `PackedArchive.members` ordered as written into the tar, so step 4 can build the index without re-sorting.
