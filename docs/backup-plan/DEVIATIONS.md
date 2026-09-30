# Spec Deviations and Resolved Ambiguities

> **2026-09-24: folded into `docs/cfa-spec.md` v1.0.** Every deviation below
> (D1-D4, D6, D9-D12; D5, D7, D8 superseded/deferred as noted inline) has been
> incorporated into the published spec, which now describes current behavior
> directly rather than deviating from it. This file stays as the historical
> record of *why* each choice was made - the spec itself doesn't carry that
> narrative. D7 (index-only restore) remains genuinely unimplemented and is
> now spec §11 (Out of Scope) rather than a deviation.

Where the implementation does not match `docs/cfa-spec.md` v2.0 literally, and why.
Each entry is either a **deviation** (the spec says X, the code does Y) or an
**interpretation** (the spec admits more than one reading; we picked one).

Intent is to fold these back into the spec at its next revision. Nothing here
has been changed in `docs/cfa-spec.md` itself — that document is Steve's.

---

## D1 — Single/part boundary is `max_size - 1 MiB`, not `max_size`

**Kind:** deviation · **Spec:** §3 table vs §9 · **Step:** 03 · **Logged:** 2026-09-17

§3's table puts the `single`/`part` boundary at `max_size`, and applies a 1 MiB
tar-overhead margin only to the split formula. §9 then requires that *no*
uploaded object exceed `max_size`. Those cannot both hold: tar adds a 512-byte
header, pads data to a 512-byte block, appends two end-of-archive blocks, and
rounds the file up to `RECORDSIZE` (10240) — about 13 KiB worst case with a pax
long-name header.

Measured: a 3,145,727-byte file with `max_size` = 3 MiB classifies as `single`
per the table and produces a **3,153,920-byte** tar, 8,192 bytes over.

**Resolution:** `packer.payload_ceiling(max_size) = max_size - 1 MiB` applies
§3's own margin to all three archive types. It is the single/part boundary, the
split chunk size, and — combined with `clump_size` — the clump seal point.

**To rectify in the spec:** change §3's table row from `min_size` to `max_size`
→ `min_size` to `max_size - 1 MiB`, and state that the 1 MiB margin applies to
every archive type rather than only to splitting.

**Test:** `worker/tests/test_packer.py::test_no_archive_exceeds_max_size`,
`::test_classify_at_max_size_is_part`

---

## D2 — Clumps seal at `min(clump_size, max_size - 1 MiB)`

**Kind:** deviation · **Spec:** §2, §3 · **Step:** 03 · **Logged:** 2026-09-17

§3 says a clump is closed once it reaches `clump_size`, and step 01's validation
permits `clump_size == max_size`. At that setting the same overhead as D1 pushes
the clump tar past `max_size`. Reproduced with two clump members near half the
ceiling: a 3,153,920-byte tar against a 3,145,728-byte `max_size`.

**Resolution:** the clump seal point is clamped to the D1 payload ceiling.

**To rectify in the spec:** note in §3 that `clump_size` is capped by the same
margin, or in §2 that `clump_size` must be at least 1 MiB below `max_size`.

**Test:** `worker/tests/test_packer.py::test_clump_never_exceeds_max_size_when_clump_size_equals_max_size`

---

## D3 — Part sizes are balanced, not chunk-sized with a remainder

**Kind:** interpretation · **Spec:** §3 · **Step:** 03 · **Logged:** 2026-09-17

§3: "split into k = ceil(size / (max_size − 1 MiB)) parts of equal size, except
that the last part may be smaller." Two readings: parts of exactly `chunk` with
a possibly-tiny remainder, or balanced parts of `ceil(size / k)`.

**Resolution:** balanced. It is what makes `k` meaningful, and it avoids a
1-byte final part when `size == k * chunk + 1`. Both readings satisfy the
sentence; either keeps every part under the margin.

**To rectify in the spec:** state the part size explicitly as `ceil(size / k)`.

**Test:** `worker/tests/test_packer.py::test_split_parts_are_balanced`

---

## D4 — Tar format is PAX, not ustar

**Kind:** interpretation · **Spec:** §3 · **Step:** 03 · **Logged:** 2026-09-17

§3 says "standard POSIX tar" and §5 says member names are the file's path
relative to its source root. Strict ustar caps member names at 100 characters,
which real media paths exceed routinely. PAX (POSIX.1-2001) is a standard POSIX
tar format and handles them; verified that `bsdtar 3.5.3` extracts a 108-character
member name written by Python's `PAX_FORMAT`, so §3's "`tar -xf` restores them"
still holds.

**Resolution:** `tarfile.PAX_FORMAT`, passed explicitly rather than relying on
the Python default.

**To rectify in the spec:** say PAX explicitly in §3, and note that ustar is
insufficient for real paths.

**Test:** `worker/tests/test_packer.py::test_tar_extracts_with_real_tar`
(includes a >100-character path)

---

## D5 — The pipeline refuses to run while backup encryption is enabled

> **Superseded by D9 (2026-09-19):** the refusal is gone; encryption is now supported. Kept for history.

**Kind:** addition · **Spec:** §8 · **Step:** 05 (planned) · **Logged:** 2026-09-17

§8 puts encryption changes out of scope, but says nothing about what happens
when MediaBridge's existing per-file encryption is switched on. §3 and §7 need
tars that `tar -xf` opens with ranged reads at plaintext offsets, which
whole-archive encryption would break.

**Resolution** (decided 2026-09-16): the v2 tar pipeline writes plain tars and
refuses to run, with a clear error, while `BackupEncryptionConfig.enabled` is
true. Today's per-file encrypted path stays in place for that case, so no
capability is lost and nothing is silently downgraded.

Note the general rule this respects: compression must precede encryption, since
ciphertext is high-entropy and will not compress. v2.0 has no compression, so
the ordering does not arise here — but it constrains any future attempt to add
either one to this pipeline.

**To rectify in the spec:** add a line to §8 saying the pipeline is mutually
exclusive with the legacy encrypted path in v2.0.

**Test:** `worker/tests/test_backup_run.py::test_refuses_when_encryption_enabled`

---

## D6 — An upload that exhausts its retries fails the whole run, not just that archive

**Kind:** interpretation · **Spec:** §6 · **Step:** 06 · **Logged:** 2026-09-18

§6 says a failed upload is retried up to 5 times with backoff, but is silent
on what happens once those attempts are exhausted: fail the whole run, or
mark that archive "skipped" and continue with the rest.

**Resolution:** `upload_and_confirm`'s `UploadVerificationError` propagates
out of `run_backup` uncaught, failing the whole task. No index and no
`BackupArchive`/`BackupRecord` rows get written for that archive (or any
later one, since the run stops there) - archives already fully recorded
earlier in the same run are unaffected. This was chosen over downgrading to
`"partial"` because §6.2's "partial" status is specifically about individual
files changing or disappearing mid-run, a qualitatively different, expected
condition; an upload that fails 5 times against a configured, reachable
bucket is not expected, and hiding it inside a "partial" summary risks it
going unnoticed.

**To rectify in the spec:** state in §6 that an exhausted upload retry aborts
the run.

**Test:** `worker/tests/test_backup_run.py::test_failed_upload_writes_no_index_and_no_rows`,
`::test_lock_released_on_failure`

---

## D7 — Restore's index-file fallback (§7) is not implemented

**Kind:** deviation (deferred) · **Spec:** §7 · **Step:** 07 · **Logged:** 2026-09-18

§7: "Look up the hash (or path) in the database, **or in the index files when
the database is unavailable**." Step 07 only implements the database lookup
(`_v2_ledger_parts` in `worker/app/tasks.py`) - there is no path that lists
`<prefix>index/` in the bucket, downloads index files, and searches them by
hash when the database can't be reached.

**Resolution:** deferred, not built. It's a disaster-recovery feature (the
database itself being unavailable, not just a single restore failing) that
needs its own design: `StorageBackend` (step 05) has no "list objects"
primitive yet, and "search every index file for a hash" has no defined
performance bound as a bucket accumulates archives. Building it opportunistically
inside step 07 risked the same kind of under-specified guess the plan's
"author steps just in time" rule exists to avoid. The database-available path
- the one every normal restore uses - is fully implemented and tested.

**To rectify in the spec:** either scope §7's fallback clause out of v2.0 (add
it to §8, Out of Scope) or give it its own step once the database-available
path has been in use long enough to know whether it's actually needed.

**Test:** none - this is what's *not* tested. Tracked in `docs/TODO.md`.

---

## D8 — Retiring `_plan_backup` is deferred; step 8 is §6.1 only

**Kind:** deviation (deferred, scoped by explicit user decision) · **Spec:**
§6 (step list) · **Step:** 08 · **Logged:** 2026-09-18

§6's own step list names two things under "step 8": skipping unchanged files
(§6.1) and retiring the old percentage-based planner
(`_plan_backup`/`_execute_backup_plan` in `web/app/main.py`). Those aren't the
same size of change - `_plan_backup` drives the UI's **per-file** bulk-backup
buttons (`/movies/bulk-backup`, `/movies/bulk-backup-cloud`), a fundamentally
different shape of work from `run_backup`'s **whole-library** runs. Retiring
it means deciding how, or whether, the UI exposes triggering a `run_backup`
run at all - a real design decision, not an implementation detail.

**Resolution** (Steve's call, asked directly rather than assumed): step 08
implements §6.1's skip-check only, inside `worker/app/backup_run.py`.
`_plan_backup`, `_execute_backup_plan`, `BackupPlanItem`, and the per-file
bulk-backup routes are untouched and keep working exactly as before. v2
backups remain triggerable only by calling `run_backup` directly (there is no
UI path to it yet) - the same "wired up but nothing enqueues it" state
step 06 left it in.

**To rectify in the spec:** note that "retire the old planner" depends on a
UI decision out of scope for the backend-only sections §1-§7, or split it
into its own step once that decision is made.

**Test:** none required - nothing here changed. `web/app/main.py` is
untouched by this step; see `docs/TODO.md`.

---

## D9 — Per-file encryption before packing (supersedes D5; touches §3, §5, §7, §8)

**Kind:** deviation (scope change, Steve's call) · **Spec:** §3, §5, §7, §8 · **Step:** 10 · **Logged:** 2026-09-19

§8 lists encryption changes as out of scope and D5 made encryption mutually
exclusive with v2. Steve decided encryption is required, so v2 now works like this:

1. Global key (existing PBKDF2 master key from the backup password).
2. SHA-256 per file (existing).
3. Each file is encrypted on its own into one blob: key = `derive_file_key(master, sha256)`
   (HMAC of the file hash under the global key), chunked AES-256-GCM, fresh random
   nonce prefix per encryption, header authenticated as AAD. Format in `encryption.py`.
4. Clumping (`< min_size`) and 5. splitting (`> max_size`) then act on the
   **ciphertext**. Tar members are named `<sha256>.file` (`.partNNNN` for parts).
   Parts are cut on encrypted-chunk boundaries so restore can stream a part at a time.
6. The per-archive index is unchanged except each entry's `paths` list is replaced by
   `paths_enc`: the paths sealed with the file's key (AAD = its hash), and the index
   carries `"encrypted": true`. Hashes, sizes, offsets, mtimes stay plaintext.
7. Upload and index-after-confirm are unchanged.

Restore: fetch parts in order, concatenate to the ciphertext blob, decrypt, verify the
plaintext SHA-256 against the record. Any tag failure, truncation, reorder or wrong key
raises before a file is written.

**Accepted by Steve, not bugs:** bucket is private so plaintext hashes/sizes/mtimes are
acceptable; deterministic per-file keys are acceptable (random nonces keep GCM safe);
no key rotation yet (a changed password makes old backups undecryptable).

**Still to do when the spec is rewritten (Steve: "update this at the end"):** §3 (member
names), §5 (`paths_enc`, `encrypted`), §7 (decrypt step), §8 (drop encryption from
out-of-scope), and D5. `docs/cfa-spec.md` itself is deliberately untouched for now.

Encryption stays optional: with it off, v2 writes plain tars exactly as before.

> **Update 2026-09-19 (D8):** the Libraries page's per-library cloud backup button now enqueues `run_backup`. Retiring `_plan_backup` for the remaining buttons is still deferred.


## D10 - Optional gzip before encryption (touches §3, §5, §7; step 14)

Files may be gzipped before they are encrypted and packed (`docs/backup-plan/steps/14-compression.md`). Departures from the spec text:
- §3: tar member names for a gzipped plain member are `<rel_path>.gz`; encrypted members are still `<sha256>.file`. A split gzipped file is `cat`-then-`gunzip`, not just `cat`.
- §5: an index entry may carry `"compression": "gzip"` (absent = as-is). Hashes stay those of the original file.
- §7: restore gunzips after decrypting, then verifies the SHA-256 as before.
- Verify (not in the spec) is sampled by default; see step 14.

Optional and off by default: with it off, or for any file that doesn't shrink by 5%, output is exactly as before. `docs/cfa-spec.md` itself is still deliberately untouched - add these to the rewrite list with D9's.


## D11 - Files whose content is already in the archive are recorded, not uploaded (touches §6, §5)

A file whose SHA-256 already has a completed backup at the same archive (a rename, move, second copy, or a touched file) gets its own `BackupRecord` pointing at the existing stored bytes instead of being packed and uploaded again (`docs/backup-plan/steps/16-cheap-sync-and-verify.md`). Departures:
- §6 step 3-5: such files skip packing/upload/index. Only reused when the existing copy is complete, has the same encrypted-or-not state, and (if encrypted) the current key version.
- §5: because nothing is written to the bucket, an archive's index file lists only the paths present when the content was first uploaded. Lookup by hash (the index's key) still finds the content; the extra paths exist only in the database. A database-less recovery therefore sees the content but not every path it was known by.
- Stored bytes are shared by several records, so any future "delete old data" feature (spec §8) must count references before removing an archive.

> **Update (step 17):** D8 is retired. `_plan_backup` and the whole legacy per-file pipeline were removed; every backup, restore and verify is now a v2 run (`docs/backup-plan/steps/17-remove-legacy.md`). References above to the "frozen legacy path" are history.

---

## D12 — Index files are same-directory sidecars, not a separate `index/` folder

**Kind:** deviation · **Spec:** §4 · **Logged:** 2026-09-23

§4 lays out `<prefix>archives/<archive_id>.tar` and `<prefix>index/<archive_id>.json` as sibling folders. Changed to `<prefix><archive_id>.tar` and `<prefix><archive_id>.json` - same directory, same basename, different extension - so browsing the bucket shows each archive next to its own index rather than split across two folders.

Only affects newly-uploaded archives: existing `archives/`/`index/`-layout objects are untouched, and the two schemes now coexist indefinitely within one archive's prefix (no migration of already-uploaded objects). Everything that reads an object's location from a stored path (not just the two key-builder functions) had to stop assuming the old layout:
- `worker/app/backup_index.py`: `archive_object_key`/`index_object_key` produce the new sidecar keys; new `index_key_for_archive_path(archive_path, archive_id)` derives an archive's index key from its own recorded `.tar` path, detecting which of the two schemes produced it (old: swap the trailing `archives/` segment for `index/`; new: same directory) - used by verify/restore instead of assuming the current scheme.
- `worker/app/tasks.py`: verify's per-archive index lookup and its batched-stat grouping (grouping by an archive's own containing folder, not a hardcoded `archives/` search) both updated accordingly.
- `web/app/cloud_inventory.py`: the bucket-listing regexes (`_match_archive`/`_match_index`) try the old `archives/`/`index/`-anchored pattern first, falling back to the new bare pattern - a single "subfolder optional" regex would greedily swallow `archives/`/`index/` into the prefix capture, making an old- and new-scheme object in the same folder pair under different prefixes and break archive-index pairing.

**To rectify in the spec:** update §4's object-key layout, and note that a real deployment may have both layouts coexisting in one prefix indefinitely (no automatic migration is planned - spec §8 excludes deleting/rewriting old data).

---

## D13 — Portability: adopting an existing archive's content into a fresh instance

**Kind:** out-of-scope feature · **Spec:** §8 (out of scope) · **Logged:** 2026-09-26

§8 explicitly excludes anything cross-instance (version history, deletion, compaction - portability
isn't named but is squarely the same category: it has no counterpart in the spec at all). Built
anyway because it's the practical answer to "reinstall MediaBridge, point it at the same bucket,
get your catalog back" - a fresh instance's database has never heard of the bucket's content, so
instead of the normal `_v2_ledger_parts` database lookup, `worker/app/bucket_inventory.py` lists
every index file under a prefix directly (the same mechanism D7's deferred index-file fallback
would have used, repurposed for "never had a row" instead of "database is unavailable") and
`worker/app/sync_run.py` resolves paths by trying the current master key plus every retired
`BackupKeyVersion`, adopting matches into `MediaFile`/`BackupRecord`/`BackupArchive`/
`BackupRecordArchive` so a future backup of that library re-uploads nothing (D11).

Full detail: `docs/backup-plan/steps/18-portability.md` (`SyncRun`, `bucket_inventory.discover`,
`sync_run._all_candidate_keys`/`_ContentResolver` (renamed from `_PathResolver` when D14 gave it a
second job, resolving sha256 as well as paths), the web `discover-bucket`/`sync-from-bucket` routes,
and the 2026-09-26 increment: `web/app/encryption_check.py`'s proactive key-match check plus the
Libraries page's auto-triggered A/B/C prompt).

**To rectify in the spec:** if ever folded in, this would need its own out-of-scope-but-documented
section (or an explicit "not addressed" note next to §8's list) rather than silence, since unlike
version history/deletion/compaction it's already built and shipping.

---

## D14 — Content-id keying: the exposed identifier is a keyed HMAC, not the real SHA-256

**Kind:** deviation · **Spec:** §5, §8 (encryption) · **Logged:** 2026-09-26

§5 keys every index entry by the file's real SHA-256, in both plain and encrypted archives - "hashes,
sizes and offsets stay readable" even when paths are sealed. That's a privacy hole for encrypted
backups specifically: SHA-256 is a public, unkeyed function, so anyone with a candidate file (a known
movie release, a leaked document) can hash it themselves and check for a match in the index without
ever touching the bucket's actual contents or needing the master key - encryption hides *what a file
is* but not *whether we have a specific one*.

Changed, for encrypted runs only: entries are now keyed by `content_id = HMAC-SHA256(master_key, sha256)`
(`worker/app/encryption.derive_content_id`) instead of the real SHA-256, and encrypted tar member names
switch from `<sha256>.file` to `<content_id>.file` to match. Every newly-built index that uses this
scheme carries a top-level `"key_scheme": "content_id_v1"` marker; its absence is what marks an index
as pre-D14 (real-SHA256-keyed) - **not a version number to compare, an unambiguous yes/no** on which
scheme produced it, since every archive written before this change lacks the field entirely. Plain
(unencrypted) runs are unaffected: there's no master key to derive a content-id from, so their indexes
keep the real SHA-256 as the key exactly as §5 describes.

The real SHA-256 keeps its existing jobs (`derive_file_key`'s input, post-restore integrity
verification) and never leaves the database - `BackupRecord.sha256` stays as-is; nothing about D14
touches its meaning. Since `content_id` doesn't need the file re-read to compute (it's an HMAC over
the already-known `sha256`), it's stored keyed by which `BackupKeyVersion` produced it, not just once:
new table `BackupRecordContentId` (`backup_record_id`, `key_version_id`, `content_id`, unique per
pair), backfilled automatically on every rotation (`web/app/key_versions.py:backfill_content_ids`,
called from `set_passphrase`) and once at deploy time for installs that already had backups before
this shipped (`web/app/main.py:on_startup`). This is what lets dedup (`_adopt_existing_copies`) keep
recognizing previously-archived content across a key rotation via an indexed lookup, instead of either
breaking at the rotation boundary or re-scanning every record's SHA-256 in Python on every backup run.

A corollary this broke and had to fix: `worker/app/bucket_inventory.py`'s database-independent
discovery (D13) previously trusted an index's dict key as the real SHA-256 directly - now false for a
content-id-keyed index, since content_id is one-way and unrecoverable from the bucket alone. Fixed by
sealing the real SHA-256 into each content-id-keyed entry's `"sha256_enc"` field
(`encryption.encrypt_sha256`/`decrypt_sha256`, keyed by `derive_sha256_seal_key(master_key)` - a
separate label from `derive_file_key`, since deriving *that* key from `sha256` would be circular:
recovering `sha256` is the whole point). Whoever's doing bucket-inventory discovery already has the
master key by construction (same instance, or another sharing it) - the privacy property holds because
someone *without* the key still can't compute or confirm anything, exactly as intended. `sync_run.py`'s
`_ContentResolver` (renamed from `_PathResolver`) resolves `sha256_enc` first, then `paths_enc` using
the now-known real SHA-256 - the same fix was needed in `web/app/encryption_check.py:check_key_match`,
the Libraries-page proactive key check, which had the identical bug.

Restore/verify never had this problem: they always start from a `BackupRecord` row that already knows
the real SHA-256, and a content-id-keyed archive's own `BackupArchive.key_version_id` (fixed at write
time, never changes) tells them exactly which key to derive `content_id` with - so an old archive
stays fully readable indefinitely regardless of how many rotations happen after it was written,
without depending on the `BackupRecordContentId` backfill table at all (that table is purely a
dedup-speed optimization).

Full detail: `worker/app/encryption.py` (`derive_content_id`, `derive_sha256_seal_key`,
`encrypt_sha256`/`decrypt_sha256`), `worker/app/backup_index.py` (`build_index`'s `key_scheme`/
`sha256_encryptor`), `worker/app/backup_run.py` (`_prepare_candidates`'s `content_id_key`,
`_adopt_existing_copies`'s content-id-keyed dedup path, `_upsert_content_id`), `worker/app/tasks.py`
(`_index_lookup_key`), `web/app/key_versions.py` (`backfill_content_ids`).

**To rectify in the spec:** §5's "hashes ... stay readable" line needs an encrypted-archive carve-out
describing content-id keying, the `key_scheme` marker, and `sha256_enc`; §8 (encryption) should note
that identifiers, not just paths, are sealed when encryption is on.

---

## D15 — Per-archive `key_check` verifier, and a rotation warning

**Kind:** deviation (addendum to D14) · **Spec:** §5, §8 (encryption) · **Logged:** 2026-09-27

D14 gave an index a way to say *how* its entries are keyed (`"key_scheme"`) but nothing said *which*
key produced them, short of trial-decrypting a real entry against every candidate password - a real
cost `sync_run.py`'s `_ContentResolver` and `web/app/encryption_check.py:check_key_match` were already
paying, once per archive (cached), for portability/discovery (D13).

Added: a per-archive verifier, `key_check = HMAC-SHA256(master_key, "mediabridge-key-check")[:8 bytes
hex]` (`worker/app/encryption.derive_key_check`, mirrored in `web/app/encryption_check.py` for the same
reason D14's other derivations are mirrored there). `backup_index.build_index()` stamps it as a new
top-level `"key_check"` field alongside `"key_scheme"`, only when the run is content-id-keyed (never
for a plain/legacy index, since there's no per-archive master key to check there). Not retroactive,
same as `key_scheme` - archives written before this shipped simply lack the field, and every consumer
treats its absence as "unknown," never as a mismatch, falling back to the D14 trial-decrypt path.

This isn't a new offline-guessing oracle: `sha256_enc`'s AEAD tag already fails loudly on a wrong key,
so trial-decryption against a real entry was already a functioning (if slower) way to test a candidate
password. `key_check` just makes that check cheap and explicit - one HMAC per candidate instead of an
AES-GCM decrypt attempt - and, unlike a trial decrypt, works even when no real content-id-keyed entry
is available to test against.

Wired into `sync_run.py:_ContentResolver._find_key_for_archive` and
`web/app/encryption_check.py:check_key_match`: both now check `index.get("key_check")` first and, only
when absent, fall back to trial-decrypting `sha256_enc` exactly as D14 already did. This changed
`bucket_inventory.Sha256Resolver`'s signature (`worker/app/bucket_inventory.py`) to pass the resolver
the whole index dict instead of just `archive_id`, since `key_check` is an index-level field, not a
per-entry one.

Separately (not a spec deviation, a UX gap D14 left open): rotating an *already-set* backup passphrase
(`web/app/templates/settings.html`'s backup-encryption form) now shows a blocking confirmation dialog
explaining that content already archived stays encrypted under the old key indefinitely - it is not
re-encrypted - and that losing that old passphrase (and its Key History row) makes that content
permanently unreadable. First-time password setup (no existing password) does not show this dialog.
Nothing about an already-written archive's own sidecar changes on rotation - its `key_check` describes
whichever key actually produced its ciphertext, permanently, exactly as `BackupArchive.key_version_id`
already fixes that at write time; patching a sidecar to "match" a new key without re-encrypting its
bytes would make it describe something false, and would conflict with the standing
never-overwrite-a-bucket-object rule.

Full detail: `worker/app/encryption.py` (`derive_key_check`), `worker/app/backup_index.py`
(`build_index`'s `key_check` param), `worker/app/backup_run.py` (computes `key_check` once per run
alongside `sha256_seal_key`), `worker/app/bucket_inventory.py` (`Sha256Resolver` signature),
`worker/app/sync_run.py` (`_ContentResolver._find_key_for_archive`), `web/app/encryption_check.py`
(`derive_key_check`, `check_key_match`'s `key_check` shortcut), `web/app/templates/settings.html`
(rotation confirmation).

**To rectify in the spec:** alongside D14's carve-out, §5 should note the optional `"key_check"` field
and its purpose; no §8 change needed beyond what D14 already requires.

