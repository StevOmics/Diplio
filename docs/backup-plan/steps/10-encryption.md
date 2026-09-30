# Step 10: Encryption before packing

- **Phase:** post-plan extension (see `DEVIATIONS.md` D9, which supersedes D5)
- **Depends on:** 03 (packer), 04 (index builder), 06 (run), 07 (restore)

## Goal
Encrypt each file, then clump/split the ciphertext, sealing paths in the plaintext index.

## What changed
- `worker/app/encryption.py`: `encrypt_blob` / `decrypt_blob` (single blob: 20-byte header `MBE1|nonce_prefix|chunk_size|plain_size`, then fixed-size GCM chunks with the header as AAD; `BLOB_CHUNK_SIZE` 64 MiB), `blob_size_for`, `encrypt_paths` / `decrypt_paths`.
- `worker/app/packer.py`: `FileToPack.member_stem` (tar member name without paths); `split_part_sizes(..., align_unit, align_head)` cuts on chunk boundaries; `pack(..., align_unit, align_head)`.
- `worker/app/backup_index.py`: `build_index(..., path_encryptor)` -> `paths_enc` + `"encrypted": true`.
- `worker/app/backup_run.py`: refusal now only for enabled-without-password; `_encrypt_candidates` (one blob per unique hash, in a temp dir dropped once the tars exist); `BackupArchive.encrypted` set.
- `worker/app/tasks.py` `_restore_v2`: encrypted archives are concatenated then `decrypt_blob`ed, then the plaintext SHA-256 is checked.

## Traps
- Encrypted disk use peaks at roughly 2x the run's data (blobs + tars) - the packer still writes every tar before any upload.
- `run_backup` still has no UI trigger (D8).
- `archive_length` for a part comes from `split_part_sizes` and must be called with the same `align_*` args as `pack` (`_split_args`).
- Legacy encrypted paths (`_store_plaintext_as_archive` etc.) are untouched and frozen.

## Tests
`worker/tests/test_encrypted_blob.py` (pure: round trip at chunk edges, tamper/truncate/reorder/wrong key, boundary-aligned parts, swapped parts, index hides paths) and `worker/tests/test_encrypted_run.py` (DB-backed: clump+single+split round trip, bucket has no plaintext names/content, wrong key fails restore).
Run DB tests against a **scratch database**, never the dev one - see `docs/backup-plan/HANDOFF.md`.
