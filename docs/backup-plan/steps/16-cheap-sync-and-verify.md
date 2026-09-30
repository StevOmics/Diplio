# Step 16: Cheap syncs and cheap verifies

- **Depends on:** 6 (backup run), 8 (skip unchanged), 14 (verify modes).
- **Why:** the archive bucket is `ARCHIVE` storage class - cheapest to store, but the priciest per request, and every byte read back is billed as retrieval. Measured on a 2000-file / 211-archive library before this step: a no-change re-sync made **0** bucket requests (skip-unchanged was already right), but a shallow verify made **7 requests per file (14 000)** and read ~3x the stored bytes, and a renamed/copied file was uploaded again in full.

**After:** shallow verify of the same 2000 files = **425 requests (1 listing + 211 index reads + 213 data reads), 48 MB read** - 2 per archive, 0.21 per file, each stored byte read about once. Live, 19 files / 10 archives = 17 requests.

## Sync: what is (and isn't) uploaded
1. **Unchanged** (section 6.1, unchanged): same path, size, mtime as the last backup -> not even hashed. A re-sync of a fully backed-up library is DB queries only - no bucket requests (`test_resync_of_an_already_backed_up_library_touches_nothing`).
2. **New: already stored** (`backup_run._adopt_existing_copies`, right after hashing, before packing): a file whose SHA-256 already has a `done` record at this archive - renamed, moved, a second copy, or merely touched - is recorded against the existing stored bytes: a new `BackupRecord` plus copies of the source's `BackupRecordArchive` links (same archive, offset, length). No upload, no bucket request. Skip reason `already_backed_up`; not a "partial" run.
   Reused only if the existing copy matches how this run would store it: same encrypted-or-not, and (if encrypted) the **current key version** - so enabling encryption or rotating the key is never bypassed - and every archive is complete (`indexed_at`).
3. Everything else is packed and uploaded as before (duplicates *within* a run were already stored once by the packer).

## Verify: cost tracks archives, not files
Shallow verify (`_verify_v2_batch`, task `verify_v2_batch`, sent by the web app in batches of 500 per archive):
- **Object metadata** (size + CRC32C - which proves every byte at rest unchanged since upload) once per archive per batch: one listing per 1000 objects when that is cheaper than a stat each (`needed > ceil(total/1000)` for the prefix), else one `stat_or_none` each. Metadata only - no retrieval fee.
- **Index files** downloaded once per archive per batch, checked against every file of that archive in memory.
- **Data samples** wanted from one archive are merged (gap <= 256 KiB) into as few ranged reads as possible - a whole clump of small files is one request. Files are processed in archive order in windows (<= 500 files / 128 MiB) so memory is bounded.
- **Sampling is budgeted.** Plain files: 1 MiB from each end. Encrypted files can only be checked a whole chunk at a time, so they are decode-sampled only when their stored size is <= 4 MiB; bigger ones rely on the CRC32C + index checks (Deep verify still reads everything). Tunable: `VERIFY_SAMPLE_BUDGET_BYTES`.
- A missing/short object is reported (`missing`/`mismatch`), never an exception that kills the batch.
- Each batch logs its cost: `verify (shallow): 19 file(s) {'match': 19} - cloud cost: 17 requests (list=1, read_object=10, read_range=6), 5724202 bytes read`.
- `StorageBackend` gained `stat_or_none`, `read_object`, `list_stats` (one request each, on the backend's own client; the old `exists()` builds a new authenticated client per call). `app.storage.CountingBackend` counts requests and bytes.

## Limits / notes
- An adopted copy's path is **not** added to the archive's `index/<id>.json` (nothing is written to the bucket); the database knows it, and the hash-keyed index entry still finds the content. See `DEVIATIONS.md` D11.
- Adoption keys on content only: it needs the hash, so a *new* path still costs one local read to hash it (never a cloud request).
- Skip-unchanged compares size/mtime with the catalog's size, not the size at backup time (no such column): a same-size-and-mtime edit, or a restored mtime with a changed size after a rescan, is not seen (spec section 6.1 accepts this).
- Records from before the v2 pipeline (no `sha256`/`mtime_ns`) are never trusted by skip-unchanged or adoption, so a library backed up only by the legacy path is uploaded once in v2 format.
- Only `list_stats` on a bucket-level listing scales with the prefix; it is bounded by `archives/` under the library's own prefix.

## Tests
`worker/tests/test_scale_sync.py` (set `MB_SCALE_FILES=20000` and `-s` for real numbers), `test_verify_batch.py`, `test_adopt_existing.py`.
