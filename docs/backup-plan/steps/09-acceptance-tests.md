# Step 09: Acceptance tests

- **Phase:** step 9 of 9 (see `docs/backup-plan/PLAN.md`) - **the last step of the plan**
- **Spec sections to read:** `docs/cfa-spec.md` §9 (the whole specification for this step - six bullets), plus §3/§4/§5/§6 for the mechanics each test is checking
- **Depends on:** 06 (backup run), 07 (restore), 08 (skip unchanged) - this step exercises all three together and adds nothing new to any of them

## Goal
One new test module, `worker/tests/test_acceptance_v2.py`, with one test per §9 bullet - six total - each exercising the real pipeline end to end (`_execute_backup_run` + `_restore_v2`, the same extracted orchestration functions steps 06-08 already test) rather than re-testing any single module in isolation. Where §9 says "extract with `tar -xf`" or "reassemble with `cat`", the test shells out to the real `tar`/`cat` binaries (both present in the worker image - confirmed via `docker compose run --rm worker sh -c "which tar cat"`), not `tarfile`/Python concatenation, because the point of those two bullets is interoperability with real tools, not just self-consistency with our own reader.

## Context
- This step **adds no new production code**. Steps 06-08 already implement everything §9 checks; this step's only job is to prove it, together, the way a real backup/restore cycle would exercise it. If a test here fails, the bug is in an earlier step's code, not something to patch inside this step.
- Reuse `worker/tests/conftest.py`'s `db_session`/`catalog`/`*_config_state` fixtures and `tests/storage_double.py`'s `LocalBackend`/`FlakyBackend`, exactly as steps 06-08 do. No test contacts a real bucket.
- **Size matrix, following D1's resolution, not §3's literal table.** The single/part boundary is `payload_ceiling(max_size) = max_size - 1 MiB` (`DEVIATIONS.md` D1), not `max_size` itself - so "a file over `max_size`" for the split case should really be "a file over the payload ceiling but comfortably under `max_size`," which is what actually exercises the part path without contradicting the "no object exceeds `max_size`" bullet. Suggested settings: `min_size=1_000`, `max_size=2_000_000` (so `payload_ceiling = 951_424`). Sizes: `0`, `999` (just under `min_size`, clump), `1_001` (just over `min_size`, single), `1_600_000` (over the payload ceiling, forces a 2-part split, still under `max_size`).
- §9's "an unchanged file is not uploaded again" and "every index entry's hash, offset, and size match the archive" bullets already have DB-level/unit-level coverage from steps 06/08. This step's versions check the same guarantees from outside - at the actual stored bytes, via the backend - which is a strictly stronger, end-to-end version of the same claim, not a duplicate.
- For the "interrupted run" bullet, use `FlakyBackend` to fail every attempt of a *later* archive's upload after an *earlier* one has already fully succeeded (uploaded, indexed, recorded) - two `"single"`-classified files with distinct `rel_path`s (so they don't clump together and pack in a predictable, alphabetical order) makes the timing controllable: `fail_uploads_on` is keyed by the backend's global 1-based upload call count, so the first file's one call succeeds and the second file's five retry attempts can all be made to fail.

## Changes

### `worker/tests/test_acceptance_v2.py` (new)

Six tests, each named for its §9 bullet:

1. `test_round_trip_size_matrix_byte_identical` - back up all four sizes above in one run; restore each via `_v2_ledger_parts` + `_restore_v2`; assert byte-identical to the originals.
2. `test_no_uploaded_object_exceeds_max_size` - same run (or a fresh one covering the same size matrix); for every `BackupArchive` row, `backend.stat(archive.path).size <= max_size_bytes`.
3. `test_archives_extract_with_real_tar_and_split_files_reassemble_with_cat` - `subprocess.run(["tar", "-xf", ...])` against the clump and single archives' stored bytes, confirming the extracted member(s) match the original content; for the part-split file, extract each part archive with real `tar -xf` into its own file, then `subprocess.run(["cat", part1, part2, ...])` (or shell redirection) and compare the result to the original content.
4. `test_index_entries_match_archive_bytes` - for every archive's index JSON: for a clump/single entry, `sha256(archive_bytes[offset:offset+size]) == hash_key`; for a part entry, extract the member via `tarfile`, concatenate all parts sharing that hash in `part` order, and confirm the concatenation's sha256 equals the hash key and its total length equals the index's (whole-file) `size`.
5. `test_interrupted_run_leaves_no_index_without_a_complete_archive` - a `FlakyBackend` fails every attempt of the second of two `"single"` archives' uploads; assert `_execute_backup_run` raises; then, over every object under the backend's `index/` prefix, assert a same-named object exists under `archives/` and that `tar -tf` (list, not extract) succeeds against it without error.
6. `test_unchanged_file_is_not_uploaded_again` - back up a file; snapshot the full set of object keys under the `LocalBackend` root; run again with nothing changed; assert the object key set is byte-for-byte identical (no new or modified object).

## Do Not Touch
- Everything under `worker/app/` - this step is tests only. If making a test pass seems to require a production code change, stop and report rather than patching around it; that would mean an earlier step has a real bug, which needs its own fix and its own note in `DEVIATIONS.md`, not a quiet accommodation here.
- `web/`, any model, `docker-compose*.yml`.

## Commands
- Run: `docker compose run --rm -e MEDIABRIDGE_TEST_DB=1 -v "$(pwd)/worker:/app" worker sh -c "pip install -q pytest==8.3.4 && pytest -x -q tests/test_acceptance_v2.py"`
- Full suites: `cd worker && pytest -x -q` then `cd web && pytest -x -q`

## Done When
- [ ] All six tests exist and pass
- [ ] Both full suites pass; the default (no-services) run is still green
- [ ] No test contacts a real bucket
- [ ] `git diff` touches only the new test file (plus `docs/`) - no file under `worker/app/` or `web/` changes
- [ ] A row added at the top of `docs/CHANGES.md`
- [ ] `docs/backup-plan/PLAN.md`'s current-state table marks the plan as done (all nine steps landed)

## Notes for the Implementer
- If a test fails, resist the urge to "fix" it by loosening the assertion or adjusting a fixture until it passes - trace the failure back to which step's code is actually wrong, since this step owns no production code of its own to blame it on.
- The two deferred items (`DEVIATIONS.md` D7, restore's index-file fallback; D8, retiring `_plan_backup`) are out of scope for every one of the six bullets above - none of them mention "when the database is unavailable" or the UI. Don't build either here.
- This is the last step in the plan. Once it's green, update `PLAN.md`'s current-state section to say so plainly, rather than leaving it reading like there's a "next step."
