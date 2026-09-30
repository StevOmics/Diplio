# Handoff: the v2 backup plan is complete

For a fresh session picking up work in this area. All nine steps are done and
committed on `backup-v1` - there is no next step in the plan. This file is
now reference material for whoever next touches this code: what exists, what
was deliberately left out and why, and the traps already paid for once.

## Reading order before touching this code

1. `docs/cfa-spec.md` — the target, v2.0, 100 lines. **Read all of it**, not the
   section you think you need. Anything in §8 is out of scope; do not build it.
2. `docs/backup-plan/PLAN.md` — current state, commit SHAs, commands, conventions.
3. `docs/backup-plan/DEVIATIONS.md` — where the code knowingly departs from the
   spec and why. Eight entries. Do not "fix" the code back toward the literal
   spec without reading these; D1 exists because §3's table and §9's guarantee
   genuinely contradict each other.
4. `CLAUDE.md` — project conventions, especially the token rules: always
   `pytest -x -q`, never dump full logs, read line ranges rather than whole files.
5. Whichever step's file in `steps/NN-*.md` is closest to the code being changed.

## Where things stand

Steps 1–5 built the pieces; step 6 wired them into a real, callable backup
task; step 7 made a v2-backed file restorable; step 8 made re-running backup
cheap on an unchanged library; step 9 proved all of it against every §9
acceptance criterion, end to end, with no bugs found:

| Module | What it gives you |
|---|---|
| `fingerprint.hash_file` | streaming SHA-256 + the changed-during-read guard |
| `packer.pack` | classify / clump / split, PAX tars, per-member offsets |
| `backup_index.build_index` | the §5 index dict, JSON-native |
| `storage.upload_and_confirm` | upload + strict size/CRC32C verify, 5 attempts |
| `storage_double.LocalBackend` | test backend with ranged reads and fault injection |
| `backup_run.run_backup` | the §6 orchestration: refuse/lock/skip-unchanged/hash/pack/upload/index/record/cleanup |
| `tasks._v2_ledger_parts` / `_restore_v2` | the §7 orchestration: v2-vs-legacy lookup, reconstruct, verify SHA-256, move into place |

`_plan_backup` and the entire existing backup pipeline still run unchanged
alongside all of this. Restore is reachable from the UI today:
`_queue_copy(..., job_type="restore")` and `copy_media_file` are the same
generic path both the legacy and v2 formats share, so no route change was
needed there - only what happens once the job reaches the worker changed.
**Backup is not**: there is still no UI path that triggers a v2 `run_backup`
run. That's not an oversight, it's `DEVIATIONS.md` D8 - retiring the old
per-file planner (`_plan_backup`, driving `/movies/bulk-backup*`) in favor of
a whole-library `run_backup` trigger is a real UI/design decision, explicitly
deferred rather than assumed. Until someone decides that, `run_backup` is
only reachable by calling it directly.

Two things remain deliberately **not** built - both logged deferrals, not
gaps found late:

- **Restore's §7 index-file fallback** (for when the database is unavailable)
  - only the database lookup path exists (`DEVIATIONS.md` D7). A
  disaster-recovery feature needing its own design; nothing in this plan
  depends on it.
- **Retiring `_plan_backup`** / any UI trigger for `run_backup` (`DEVIATIONS.md`
  D8), as above.

If either of those becomes a real ask, treat it as new work with its own step
file, not a patch to something here - both were deferred specifically because
folding them in opportunistically risked the kind of under-specified guess
this plan's discipline exists to avoid.

While writing step 6's tests, a **pre-existing, unrelated** schema drift
surfaced: `worker/app/models.py`'s `CloudStorageConfig` and `MediaFile` are
each missing columns the real table has `NOT NULL` with no server-side
default (`provider`/`connected`/`is_backup_target` on the former;
`extension`/`media_type`/`watched`/`play_count` on the latter). This has never
mattered because worker code only ever reads those tables' existing rows -
this note exists so the next person who has worker code *insert* a fresh row
in either table isn't surprised by an `IntegrityError`. See the TODO entry;
no model file was touched anywhere in steps 6-9 (`compare_model`'s
`worker_missing` list already treats it as an intentional subset, so nothing
here changes that tool's behavior).

## If picking this back up

There's no "next step" to author. The two deferred items above (D7, D8) are
the most likely reasons to return here, and each deserves its own fresh scope
- write a new step file (`steps/10-*.md` or similar) rather than reopening a
closed step, and re-read `docs/cfa-spec.md` in full first since it may have
been revised (the deviations in `DEVIATIONS.md` were all written with an eye
toward being folded back into the spec at its next revision).

The discipline that got the nine steps here without a bad guess shipping is
worth keeping for whatever comes next: two concrete cases from this project
where writing a step file before its dependencies landed produced a guess
that turned out wrong - step 3's step file specified a test that was
arithmetically impossible, and step 5's specified an index that would have
been silently non-unique on a fresh install. Both were caught only because
the implementer read the code rather than trusting the prose.

## How each step runs

One step per session, from a clean context. Tests first, then implementation.

1. Read the step file and the spec sections it cites.
2. Write the named tests, then make them pass.
3. Verify: both suites green (`cd worker && pytest -x -q`, `cd web && pytest -x -q`),
   `git status --short` limited to the files the step names, and read your own
   diff to confirm nothing out of scope moved.
4. Add a row at the top of `docs/CHANGES.md`. Add a `DEVIATIONS.md` entry if the
   code departs from the spec.
5. Update `PLAN.md`'s current-state table.
6. One commit per step. **Never `git push`** — it is denied in
   `.claude/settings.json` and pushing is Steve's call.

Test environment: no `pytest` on the global PATH. Use
`/private/tmp/mb-web-venv/bin/pytest` and `/private/tmp/mb-worker-venv/bin/pytest`.
Database-backed tests need `MEDIABRIDGE_TEST_DB=1` and the `db` service. A
container check needs `docker compose build web` **first** — `web` has
`build: ./web` with no source bind-mount, so `up -d` alone reruns the old image
and startup `ALTER TABLE` guards never execute.

## Standing constraints

- **Mirror every model change** in `web/app/models.py` *and* `worker/app/models.py`.
  The services share a database but are not a shared package. Use
  `assert_columns_mirrored` from `web/tests/model_sync.py`; never assert
  `compare_model(...).is_clean`, because the worker mirrors only a subset on purpose.
- **The legacy path stays working.** `_ledger_parts`,
  `_stage_gcs_encrypted_backup`, `_materialize_archive`, `_restore_from_parts`
  in `worker/app/tasks.py` are frozen. New format, new functions.
- **Never call `gcs.delete_blobs_with_prefix` from the v2 pipeline.** It deletes
  the previous object set before re-uploading; the v2 pipeline writes to fresh
  UUID keys.
- **No new tables** (§6.6). Archives and files go in the existing
  `BackupArchive` / `BackupRecord` / `BackupRecordArchive`.
- **No test contacts a real bucket.** `LocalBackend` and friends live under
  `tests/` for that reason.
- Schema changes are additive nullable columns plus an idempotent
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` guard in `on_startup`. There is no
  migration tool and this work does not need one.

## Traps steps 6-8 had to get right

1. **`BackupRecordArchive.archive_length` for a part member.**
   `PackedMember.size_bytes` is the *whole file's* size even on a part, because
   §5 keys index entries by the whole file's hash. It is **not** the member's
   byte length. Step 6 recovers the part's own length with
   `packer.split_part_sizes(size, max_size=...)[part - 1]` when recording it.
   Storing the whole-file size there is a restore bug that surfaces months
   later. Correction from the step 6 session's version of this note: step 7
   does *not* then turn around and read `archive_length` back to know how
   many bytes to fetch for a part - see trap 3.
2. **The advisory lock must use a dedicated connection**, not the ORM session's.
   Advisory locks are session-scoped; if SQLAlchemy returns the session's
   connection to the pool mid-run, the lock leaks onto a pooled connection and
   every later run is blocked until the worker restarts. (A related, smaller
   one step 6 hit in practice: a record's *old* `BackupRecordArchive` rows must
   be cleared once per run, not once per archive - a split file's parts each
   trigger their own `_record_archive` call for the same record, and clearing
   on every call deletes the earlier part's row the same run just wrote.)
3. **Restoring a part member does not trust `archive_length` (or the index's
   `size`) for its byte count at all.** Both exist for traceability, not as
   restore's source of truth. §5's index has no way to carry a part's true
   length (see backup_index.py's comment), so step 7 downloads the whole part
   object and lets `tarfile`'s own header parsing determine the exact payload
   - the tar format itself is self-describing here, so there's no need to
   trust a number computed elsewhere. Only clump/single members use
   `archive_offset`/`archive_length` for a ranged read, because for those two
   shapes a tar's raw payload bytes at that exact range *are* the plain file
   content, with no tar parsing involved.
4. **A failed SHA-256 verification on restore must raise, not degrade to a
   logged warning.** It means the stored bytes don't match what was backed
   up - exactly the failure mode this whole pipeline exists to catch. Also
   applies to a record with *no* `sha256` to check against: that's not "no
   verification needed," it's an unexpected, incomplete record, and should
   fail the same way a mismatch does.
5. **The §6.1 skip-unchanged check needs both size and mtime to match, not
   either.** `and`, not `or` - a bug here either re-backs-up everything every
   run (defeating the point of the step) or, worse, silently skips a file
   whose size actually changed because its mtime happened to be preserved
   (e.g. a restore or `cp -p`). Also: fetch every relevant `BackupRecord` for
   the destination in one query before the per-file loop, not one query per
   file - the whole point of this step is that it runs on every backup
   against a potentially large library.

## When to stop rather than guess

Stop and report if the step file contradicts the spec, contradicts the code, or
specifies something arithmetically impossible. All three have happened here and
each time stopping was right. A wrong offset, a wrong byte length, or an upload
verified as good when it is corrupt are the failure modes that do not show up
until someone needs a restore.

## Running the DB-backed tests safely (added 2026-09-19)

The fixtures delete and re-insert the singleton config rows, so they must **not** run against the dev database: a teardown failure once deleted the real `cloud_storage_config` row (recovered from dead tuples). Use a scratch DB built from the **web** models (the fixtures insert raw SQL for that schema):

```
docker compose exec db psql -U $POSTGRES_USER -c "create database mb_test"
docker compose run --rm --no-deps web sh -c 'export DATABASE_URL="${DATABASE_URL%/*}/mb_test"; python -c "import app.models; from app.db import Base, engine; Base.metadata.create_all(engine)"'
docker compose run --rm --no-deps -e MEDIABRIDGE_TEST_DB=1 -v "$PWD/worker:/src" -w /src --entrypoint sh worker -c 'export DATABASE_URL="${DATABASE_URL%/*}/mb_test"; pip install -q pytest; python -m pytest -x -q tests'
```

