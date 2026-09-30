# Step 04: Index file builder

- **Phase:** step 4 of 8 (see `docs/backup-plan/PLAN.md`)
- **Spec sections to read:** `docs/cfa-spec.md` §5 (Index File — the whole specification for this step), §4 (object layout), §7 (what restore needs from it)
- **Depends on:** 03 (`PackedArchive`, `PackedMember`)

## Goal
One pure function turns a `PackedArchive` into the §5 index dict, ready for `json.dumps`. **Nothing writes it anywhere yet** — step 5 uploads it.

## Context
- §5 is about twenty lines and is the entire specification. Read it before anything else.
- §4: the archive lives at `<prefix>archives/<archive_id>.tar` and its index at `<prefix>index/<archive_id>.json`. The index's `object` field holds the **archive's** key, with no bucket name in it — the bucket is not part of any index field.
- Step 03 produced `PackedArchive` (`archive_id`, `archive_type`, `local_path`, `size_bytes`, `members`) and `PackedMember` (`sha256`, `paths`, `size_bytes`, `mtime_ns`, `member_name`, `offset`, `part`, `part_count`) in `worker/app/packer.py`. Read those dataclasses before starting.
- `PackedMember.size_bytes` is already **the whole file's size, even on a part member**. That is what §5 requires; do not substitute the part's own byte count.
- The repo's existing timestamp convention is `datetime.now(timezone.utc)` (`worker/app/tasks.py:648`). This step takes the timestamp as an argument instead, so the output is deterministic and testable.

## Changes

### `worker/app/backup_index.py` (new, ~70 lines)
Worker-only. No imports from `app.models`, `app.db`, `app.gcs`, or `app.config` — pure logic over its arguments, so it stays in the dependency-free suite. Importing from `app.packer` is expected and fine.

```python
INDEX_VERSION = 1

def rfc3339(dt: datetime) -> str
def archive_object_key(archive_id: str, prefix: str | None) -> str
def index_object_key(archive_id: str, prefix: str | None) -> str
def build_index(archive: PackedArchive, *, prefix: str | None,
                created_at: datetime) -> dict
```

**`rfc3339`** — `"%Y-%m-%dT%H:%M:%SZ"` in UTC, whole seconds, matching §5's `"2026-01-02T03:04:05Z"`. Convert any aware datetime to UTC first; raise `ValueError` on a naive one rather than guessing its zone.

**`archive_object_key` / `index_object_key`** — `f"{prefix or ''}archives/{archive_id}.tar"` and `f"{prefix or ''}index/{archive_id}.json"` per §4. `prefix=None` must yield `archives/<id>.tar` with **no** leading slash. Step 01's `normalize_prefix` already guarantees a stored prefix ends in `/` and has no leading `/`, so no re-normalizing here — but do not assume a caller passed a normalized value blindly; if the prefix is non-empty and does not end in `/`, append one.

**`build_index`** — returns exactly this shape (§5):
```python
{
    "archive_id": archive.archive_id,
    "object": archive_object_key(archive.archive_id, prefix),
    "type": archive.archive_type,              # "clump" | "single" | "part"
    "created_at": rfc3339(created_at),
    "size": archive.size_bytes,
    "files": {
        member.sha256: {
            "paths": list(member.paths),       # sorted; >1 when deduped
            "size": member.size_bytes,         # WHOLE file size, always
            "mtime": rfc3339(from mtime_ns),
            "member": member.member_name,
            "offset": member.offset,
            # part members only:
            "part": member.part,
            "parts": member.part_count,
        }
        for member in archive.members
    },
}
```
- `"v": INDEX_VERSION` as the first key, per §5's example.
- `mtime` comes from `member.mtime_ns // 1_000_000_000` — floor division, truncating rather than rounding, then `datetime.fromtimestamp(secs, tz=timezone.utc)`. The precise nanoseconds stay in `BackupRecord.mtime_ns` (step 02); §6.1's skip-check reads the database, not the index, so truncation here loses nothing that matters.
- Omit `part` and `parts` entirely for clump and single members — do not emit `None`. §5 says a part entry "also carries" them, which means their absence is the normal case.
- Assert that no two members in one archive share a `sha256`. Step 03 deduplicates by hash, so a collision here means a packer bug, and silently overwriting a dict key would lose a file.
- The return value must be JSON-native throughout: `str`, `int`, `list`, `dict` only. No `Path`, no `datetime`, no dataclass.

## Do Not Touch
- `worker/app/packer.py`. This step consumes its output; if something is missing, report it rather than widening the dataclasses.
- `worker/app/tasks.py`, `worker/app/gcs.py`, `worker/app/encryption.py`, `web/app/main.py`, any model, route, or template.
- No upload, no file writing, no `json.dumps` call in production code — `build_index` returns a dict and step 5 serializes it.

## Tests to Write First
`worker/tests/test_backup_index.py` (new, pure logic, `tmp_path` only for building real archives via `pack`).

Build the inputs by calling the real `pack` from step 03 rather than hand-constructing `PackedArchive` objects, so the test exercises the actual contract between the two modules.

Shape, one per archive type:
- `test_clump_index_matches_spec_shape`: assert the full dict against an expected literal — `v`, `archive_id`, `object`, `type`, `created_at`, `size`, and a `files` entry with exactly the keys `paths`, `size`, `mtime`, `member`, `offset`
- `test_single_index_matches_spec_shape`
- `test_part_index_carries_part_and_parts`: every entry has `part` (1-based) and `parts` (= k)

The three details settled with Steve:
- `test_part_entry_size_is_whole_file_not_part_size`: for a file split into k parts, **every** part entry's `size` equals the original file's size, not that part's byte count (§5: "The hash is that of the whole file")
- `test_mtime_is_rfc3339_utc_seconds`: a known `mtime_ns` renders as `"YYYY-MM-DDTHH:MM:SSZ"`; sub-second nanoseconds are truncated, not rounded
- `test_created_at_is_injected_not_clock`: two calls with the same `created_at` produce identical dicts

Keys and paths:
- `test_files_keyed_by_lowercase_hex_sha256`: every key is 64 lowercase hex characters and equals the member's `sha256`
- `test_deduped_entry_lists_both_paths_sorted` (§5)
- `test_clump_and_single_entries_omit_part_keys`: `"part" not in entry`
- `test_object_key_uses_prefix`: `prefix="server01/"` → `"server01/archives/<id>.tar"`
- `test_object_key_without_prefix_has_no_leading_slash`: `prefix=None` → `"archives/<id>.tar"`
- `test_object_key_appends_missing_trailing_slash`: `prefix="server01"` → `"server01/archives/<id>.tar"`
- `test_index_object_key`: `prefix="server01/"` → `"server01/index/<id>.json"`

Robustness:
- `test_index_is_json_round_trippable`: `json.loads(json.dumps(build_index(...)))` equals the original dict. This is the test that catches a stray `Path` or `datetime` here instead of in step 5.
- `test_rfc3339_rejects_naive_datetime`: `pytest.raises(ValueError)`
- `test_duplicate_hash_in_one_archive_raises`: hand-build a `PackedArchive` with two members sharing a `sha256` and assert it raises

## Commands
- Run: `cd worker && pytest -x -q tests/test_backup_index.py`
- Full suites: `cd worker && pytest -x -q` then `cd web && pytest -x -q`
- No Docker and no database are needed for this step.

## Done When
- [ ] All listed tests exist and pass
- [ ] Both full suites pass with no services running
- [ ] `worker/app/backup_index.py` imports nothing from `app.models`, `app.db`, `app.gcs`, or `app.config`
- [ ] `git diff` shows no change to `packer.py`, any task, model, route, or template
- [ ] A row added at the top of `docs/CHANGES.md` (`260917 | ...`)

## Notes for the Implementer
- **Read §5 first**, then the two dataclasses in `packer.py`. This step is a data transformation with no cleverness in it; the risk is getting a field's meaning wrong, not the code.
- The field most likely to be got wrong is `size` on a part entry. The plausible-looking choice — the part's own byte count — silently breaks reassembly, and §5 is explicit that the hash and therefore the entry describe the whole file. There is a test named for exactly this; do not "fix" it.
- **A known gap, for step 6's benefit, not this step's:** §5 carries `offset` but no member byte-length. For clump and single members that is fine, since `size` *is* the member's length. For a part member it is not — `size` is the whole file. A part archive holds exactly one member, though, so restore downloads the whole part object rather than doing a ranged read into it. Leave the index literal to §5; note the consequence in a comment so step 6 does not attempt a ranged read of a part using `size`.
- Emit `v` as the first key. Dicts preserve insertion order and the golden-literal tests will compare equal regardless, but the serialized file is meant to be read by humans.
- Do not add a `bucket` field or parameter. The bucket is not part of any §5 field; `object` is bucket-relative.
