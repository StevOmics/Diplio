# Step 14: Optional compression, and sampled verify

- **Depends on:** 6 (backup run), 7 (restore), 10 (encryption), 13 (archives). Extends the v2 pipeline only; the legacy local-copy path is untouched.

## Pipeline

```
file --sha256(original)--> gzip --> AES-GCM blob --> tar member --> clump / single / part archive
        (unchanged)       (new,     (existing,        (existing)
                          optional)  optional)
```

The SHA-256 is always over the *original* file, so dedupe, skip-unchanged, the encryption key (derived from that hash) and restore's final hash check are unchanged. Compression happens before encryption (ciphertext doesn't compress).

## Decisions

| | |
|---|---|
| Format | Standard gzip (RFC 1952), deterministic (no name/mtime), stdlib `gzip`. `gunzip` reads it. |
| Setting | `TransferConfig.compression_enabled` (default off) + `compression_level` 1-9 (default 6). Global; Settings > Compression. Per-library is a follow-up. |
| Skip rule | Never compress known-compressed formats (video, most image/audio, zip-based docs - `compression.ALREADY_COMPRESSED_EXTENSIONS`); otherwise gzip a 1 MiB head (+ middle for big files) and skip if it saves < 5%; after a full compress, keep the `.gz` only if it saved >= 5%. Otherwise the file is stored as-is. |
| Recorded per file | `BackupRecord.compression` (`NULL` or `gzip`) and `"compression": "gzip"` on the index entry (absent = as-is). Restore and verify follow the record, never the current setting, so toggling later cannot break old backups. |
| Tar member names | Plain: `<rel_path>.gz`. Encrypted: still `<sha256>.file`. |
| Sizing | The packer classifies (clump / single / part) by the *prepared* size (gzip, then encrypted), like it already did for encrypted blobs. Split parts of a `.gz` are plain byte ranges; encrypted blobs still cut on chunk boundaries. |
| Toggling on | Applies to new/changed files only; skip-unchanged does not re-upload anything already backed up. |
| Temp disk | Per file: gzip, encrypt, delete the `.gz`. Peak is the prepared data plus tars, and compression shrinks it. |

## Where things are
- `worker/app/compression.py` (pure): `gzip_file`, `gunzip_file`, `compress_if_worthwhile`, `looks_incompressible`, plus sampled-verify helpers (`gzip_trailer`, `decompress_prefix`, `crc32_and_size`).
- `worker/app/backup_run.py`: `_prepare_candidates` (was `_encrypt_candidates`) compresses then encrypts; `_record_archive` writes `record.compression`.
- `worker/app/packer.py`: `FileToPack/PackedMember.compression` carried through; `backup_index.build_index` writes it.
- `worker/app/tasks.py`: `_unwrap_stored_bytes` (decrypt then gunzip, shared by restore and deep verify); `_restore_v2` uses it.
- Web: models + startup `ALTER`s (`transfer_config.compression_*`, `backup_records.compression`), `POST /settings/compression`, a `(gz)` mark on the catalog's backed-up pill.

## Verify: sampled by default, deep on request
`_verify_v2(..., deep=False)`; `verify_v2_backup(media_file_id, destination_id, deep=False)`; routes take `?deep=true`; the catalog has a **Deep verify** button beside **Verify**.
1. (both) each archive object exists, and its size and CRC32C match the database - GCS computes CRC32C server-side, so this covers every byte at rest without downloading.
2. (both) each `index/<id>.json` exists and agrees with the database (archive id, object key, offset, compression).
3. **Sampled:** read the *start and end* of the stored data by ranged reads (never a whole download, even for a split file). Encrypted: check the blob header and total length, then decrypt the first and last chunk (GCM authenticates them - proves key and format). Gzip: magic bytes, trailer, and the head decompresses. If the local file is unchanged: the samples must agree with it (head/tail bytes; for gzip the trailer's CRC32 and length, and the decompressed prefix).
   **Deep:** download everything, decode, compare SHA-256 to `BackupRecord.sha256`.
4. (both) local file edited since the backup -> `changed`; the bucket copy is fine.

Sampling cannot see a corrupt *middle* that leaves the object's CRC32C intact - only a bug on our side that wrote bad bytes and recorded their CRC could do that. Deep exists for that case (`test_deep_verify_catches_corrupt_middle_that_sampling_cannot_see`).

## Tests
`worker/tests/test_compression.py` (pure), `worker/tests/test_backup_compression.py` (DB-backed, scratch DB): round trips plain/encrypted x clump/single/split, member naming, index field, skip rules, old backups still restoring after the setting flips, corrupt gzip rejected, sampled and deep verify, damage at either end without the CRC, split-file samples read only the ends.
