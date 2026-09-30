# MediaBridge Backup: Implementation Plan

Target: `docs/cfa-spec.md` v2.0 (2026-09-16), which replaces all earlier versions.
Mode: manual. Steve requests one step at a time. Nothing is scripted.
Branch: `backup-v1`. One commit per step. Never push.

---

## Current state (2026-09-18) — plan complete

**All nine steps done.** 143 worker (110 dependency-free + 33 needing
`MEDIABRIDGE_TEST_DB=1`) + 72 web tests pass. This backend-side rewrite of
the backup pipeline (`docs/cfa-spec.md` v2.0, §1-§7 and §9) is implemented
and tested end to end.

| Step | Commit | What landed |
|---|---|---|
| 1 | `a7e6d30` | `TransferConfig.min_size_bytes`/`clump_size_bytes`/`max_size_bytes`, `CloudStorageConfig.prefix`, `StorageLocation.exclude_globs`; `POST /settings/backup-archiving` + Settings section; `web/tests/model_sync.py` fixture |
| 2 | `772e2bd` | `sha256_file`, `hash_file`, `HashedFile`, `FileChangedDuringRead` in both `fingerprint.py` copies; `BackupRecord.sha256` (indexed) + `mtime_ns` |
| 3 | `009313d` | `worker/app/packer.py` — `classify`, `split_part_sizes`, `payload_ceiling`, `pack`; PAX tars, per-member offsets read back from the sealed tar |
| 4 | `eddc82a` | `worker/app/backup_index.py` — `build_index`, `rfc3339`, `archive_object_key`, `index_object_key`; returns a JSON-native dict, injected `created_at` |
| 5 | `3f62470` | `worker/app/storage.py` — `StorageBackend` protocol, `GCSBackend`, `upload_and_confirm` (strict size+CRC32C verify, 5 attempts); `LocalBackend`/`FlakyBackend`/`CorruptingBackend` doubles; `BackupArchive.archive_id`/`archive_type`/`crc32c`/`indexed_at` |
| 6 | `30c4b12` | `worker/app/backup_run.py` — `run_backup` Celery task: refuse-if-unusable (encryption/cloud-config/backup-target), a PostgreSQL advisory lock on a dedicated connection, catalog read + exclude-glob filtering, hash/pack/upload/index/record/cleanup in §6 order; registered in `celery_app.py`. `worker/tests/conftest.py` (new) — the first DB-backed test fixtures in this repo, gated on `MEDIABRIDGE_TEST_DB=1` |
| 7 | `e58350e` | `worker/app/tasks.py` extended — `_v2_ledger_parts`, `_restore_v2_member_bytes`, `_restore_v2`; `copy_media_file`'s restore branch tries the v2 lookup before falling back to the untouched legacy chain. Index-file fallback (§7, DB unavailable) deferred - see `DEVIATIONS.md` D7 |
| 8 | `645acb6` | `worker/app/backup_run.py`'s `_build_file_list` skips a file whose current size+mtime both match `MediaFile.size_bytes`/`BackupRecord.mtime_ns` from its last v2 backup (§6.1), fetching all relevant `BackupRecord`s in one query. Retiring `_plan_backup` (the UI's per-file bulk-backup planner) deferred by explicit decision - see `DEVIATIONS.md` D8 |
| 9 | (pending commit) | `worker/tests/test_acceptance_v2.py` (new, no production code) - the six §9 acceptance tests end to end: size-matrix round trip, no object over `max_size`, real `tar`/`cat` interop, index-vs-archive-bytes consistency, an interrupted multi-archive run, an unchanged second run writing nothing new. All six passed against the existing implementation on the first run |

The original step 5 was split into 5 (storage layer) and 6 (run orchestration), so the plan is now nine steps, all landed.

**What "done" means here, precisely:** the backup *run* and *restore* are
fully implemented, tested, and provably correct against every §9 acceptance
criterion - but **there is still no UI path that triggers a v2 `run_backup`
run**. That's not an oversight; it's `DEVIATIONS.md` D8, an explicit decision
that retiring the old per-file planner is a UI/design question outside a
backend-only plan, to be picked up as its own piece of work when Steve wants
it. Until then, `run_backup` is reachable by calling it directly (e.g. from a
shell or a one-off Celery invocation), and the legacy pipeline (`_plan_backup`,
percentage-based settings, the old restore chain) keeps working unchanged
for anyone still using it. Restore *is* already reachable from the UI, since
it reuses the existing generic `job_type="restore"` dispatch.

The other deferral, D7 (restore's index-file fallback for when the database
itself is unavailable), is a disaster-recovery feature nothing in this plan
depends on - flag it if it ever becomes a real ask.

**Handoff brief for a fresh session:** `HANDOFF.md` — reading order, the
per-step routine, standing constraints, and the traps hit across steps 6-9.
Useful context for future work in this area even with the plan itself closed.

**Read before touching anything in this area:** `docs/cfa-spec.md` (100
lines, read it all), this file, `DEVIATIONS.md`, and whichever step's file in
`steps/` is closest to the code being touched.

**Deviations from the spec** are logged in `DEVIATIONS.md` — eight entries,
the load-bearing one being that the single/part boundary is `max_size - 1 MiB`
rather than §3's literal `max_size`, because §3's table and §9's guarantee
contradict each other. `docs/cfa-spec.md` itself is unmodified; these are
worth folding back in at its next revision.

---

## What v2.0 changed

v2.0 is much smaller than v1.0, and the removals matter more than the additions:

- **Encryption is out of scope** (§8). No key management, no format change, no KCV.
- **No version history, deletion, GC, or compaction** (§8). A backup is current state, not a timeline.
- **No new tables.** §6.6 records archives and files in the **existing** `BackupArchive` and `BackupRecord` tables.
- **No bucket opacity.** Index files are plaintext JSON keyed by SHA-256, and tar member names are the file's real relative path (§3, §5). v1.0 required the exact opposite, so anything about anonymized headers or keyed hashes is dead.
- **Everything goes in a tar**, including single-file archives (§1.3). That is new: today a plain backup is a bare file copy.

The whole v1.0 plan (30 steps, format v2, GC, four new tables) is deleted. Prior analysis that survives is folded in below.

## What the existing code already gives us

| Need | Already there |
|---|---|
| Traceability: archive → file → byte range | `BackupRecordArchive` has `part_index`, `archive_offset`, `archive_length` — models clump, single, and part uniformly, which is exactly §5's index entry |
| Archive and file records | `BackupArchive`, `BackupRecord` (`web/app/models.py:120,148`) |
| GCS upload with throttling | `worker/app/gcs.py` — `upload_file`, `download_file`, `blob_exists` |
| Clump/split planning | `web/app/main.py:_plan_backup` — decides clump vs split vs plain today, on percentage-based settings that §2 replaces with absolute sizes |
| Queue for slow file work | the `copy` queue and `worker` service |
| Pure-logic test style | both suites are dependency-free; steps 3 and 4 below stay that way |

Schema additions needed, in total: `BackupArchive.archive_id` (uuid), `.archive_type` (`clump`/`single`/`part`), `.crc32c`, `.indexed_at`; `BackupRecord.sha256`, `.mtime_ns`. All nullable and additive, so the repo's existing startup `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` convention (`web/app/main.py:80`) is enough. **No migration tool needed for this work** — `docs/TODO.md` still tracks that gap on its own merits.

## Commands

| Purpose | Command |
|---|---|
| Worker tests | `cd worker && pytest -x -q` |
| Web tests | `cd web && pytest -x -q` |
| One file | `cd worker && pytest -x -q tests/test_packer.py` |
| Rebuild a service | `docker compose build worker && docker compose up -d worker` |

`pytest -x` always, per CLAUDE.md.

## Conventions

- New modules: `worker/app/packer.py` (classify, tar, split), `worker/app/backup_index.py` (index JSON), `worker/app/backup_run.py` (orchestration). Restore extends `worker/app/tasks.py`.
- Mirror every model change in `web/app/models.py` **and** `worker/app/models.py` in the same step.
- Steps 3 and 4 are pure logic with no database, bucket, or Celery involvement. Keep them that way — they hold the parts most likely to be wrong (offsets, split arithmetic) and they should be testable with `pytest` and nothing running.
- The legacy restore path (`_ledger_parts`, `_stage_gcs_encrypted_backup`, `_materialize_archive`, `_restore_from_parts`) stays working. New format, new functions.

## Settled decisions

**Encryption interaction (decided 2026-09-16).** §8 puts encryption changes out of scope, and §3/§7 need archives that `tar -xf` opens with ranged reads at plaintext offsets — which whole-archive encryption would break. So: **the v2 tar pipeline writes plain tars and refuses to run, with a clear error, while `BackupEncryptionConfig.enabled` is true.** Today's per-file encrypted path stays in place for that case, so no capability is lost and nothing is silently downgraded. Step 6 enforces the refusal.

---

## Steps

Nine, in dependency order. Each is one request, one commit, and leaves the suite green.

### 1. Settings
§2. Replace the percentage-based knobs with `min_size` (2.5 MiB), `clump_size` (64 MiB), `max_size` (1 GiB) on `TransferConfig`; `prefix` on `CloudStorageConfig` (`bucket_name` exists); sources and exclude globs on `StorageLocation`. Startup `ALTER TABLE` guards, both model files, Settings UI fields. Keep the old columns readable until step 8 stops using them.

### 2. SHA-256 and safe reads
§1.2, §6.2. Streaming `sha256_file(path)` in `worker/app/fingerprint.py` (mirror in web). Stat before and after; if size or mtime moved, skip the file and log it. Add `BackupRecord.sha256`. Keep the existing partial-BLAKE2b fingerprint — it stays the catalog's fast identity; SHA-256 is the backup's.

### 3. Packer — pure logic
§3. Classify by size into `clump` / `single` / `part`. Clump in path order until `clump_size`, leftovers into a final smaller clump. Split with `k = ceil(size / (max_size - 1 MiB))`, equal parts with a possibly-smaller last one, members named `<relpath>.partNNNN` from 0001. Build POSIX tars into a temp dir and return each member's data offset. No compression, no anonymization — member names are real relative paths.

### 4. Index builder — pure logic
§5. Build the index dict from a packer result: keyed by SHA-256, with `paths` (a list, so two identical files in one run share one stored copy), `size`, `mtime`, `member`, `offset`, plus `part`/`parts` for part archives. Plus `archive_id`, `object`, `type`, `created_at`, `size`.

### 5. Storage backend and verified upload
§6.4, §4. `worker/app/storage.py`: a `StorageBackend` protocol with `GCSBackend` and a `LocalBackend` test double (ranged reads, CRC32C, fault injection), plus `upload_and_confirm` — upload, then verify the stored object's size and CRC32C, retrying up to 5 times with backoff. Also the four `BackupArchive` columns the run needs (`archive_id`, `archive_type`, `crc32c`, `indexed_at`). Split out of the original step 5 so step 6's orchestration has a tested storage layer beneath it. No test touches a real bucket.

### 6. Backup run
§6. `worker/app/backup_run.py`: take the Postgres advisory lock (one run at a time), refuse to run while `BackupEncryptionConfig.enabled` is true (see `DEVIATIONS.md` D5), read the file list from the catalog with the step-1 exclude globs applied, hash, pack, `upload_and_confirm`, **then** write `<prefix>index/<uuid>.json`, then record the `BackupArchive` / `BackupRecord` / `BackupRecordArchive` rows and set `indexed_at`, then delete the temp file. **The first step that changes runtime behaviour.**

### 7. Restore
§7. Look up by hash or path in the database, falling back to reading index files. Ranged read at `offset` for a clump or single member, whole-object download for a part (§5 carries no member length, and `size` is the whole file — see the comment in `backup_index.py`). Extract, concatenate parts in order, verify SHA-256, then move into place. An archive with `indexed_at IS NULL` is incomplete and is skipped.

### 8. Skip unchanged files
§6.1. Skip any file whose path, size, and mtime match the last successful backup, using `BackupRecord.mtime_ns` written by step 6. Done. Retiring the old percentage-based planner (`_plan_backup`) was deferred by explicit decision - see `DEVIATIONS.md` D8 - since it's a UI/design question (whole-library `run_backup` runs vs. today's per-file selection), not an implementation detail of §6.1.

### 9. Acceptance tests
§9. The six the spec names, end to end: the size matrix (0 bytes, just under and just over `min_size`, over `max_size`) round-trips byte-identical; no object exceeds `max_size`; archives extract with real `tar -xf` and split files reassemble with `cat`; every index entry's hash, offset, and size match the archive bytes; an interrupted run leaves no index file without a complete archive; an unchanged file is not re-uploaded.

---

## Notes carried over from the earlier analysis

Two of these are live bugs in the current backup code, independent of this work:

- **`worker/app/encryption.py` stores `chunk_count` in an unauthenticated plaintext manifest.** Editing it downward yields a silently truncated restore. Encryption is out of scope for v2.0, so this stays open — tracked in `docs/TODO.md`.
- **`worker/app/gcs.py:105 delete_blobs_with_prefix` deletes the previous chunk set before re-uploading.** Destructive if the upload then fails. The v2 pipeline writes to fresh UUID keys and must never call it.
- Step 2's SHA-256 means the first run after this lands reads every file once. Step 7's skip logic makes it a one-time cost.
- `docs/cfa-reference-spec.md` (1,902 lines) is historical background only, and `cfa-agent-kit/docs/cfa-spec.md` is the superseded v1.0 — don't plan from either.
