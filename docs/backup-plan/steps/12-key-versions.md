# Step 12: Backup key versions

- **Depends on:** 10 (encryption), 11 (run tracking). Refines `DEVIATIONS.md` D9's "no key rotation": changing the key is allowed and non-destructive; automatic re-encryption is still not built.

## What exists
- `BackupKeyVersion` (web + worker models): every passphrase + salt with `created_at` / `retired_at`; NULL `retired_at` = current. `BackupEncryptionConfig` still holds the current key for the worker.
- `web/app/key_versions.py`: `set_passphrase` (retire old, add new, no-op if unchanged), `ensure_current_version` (backfills installs that predate versions), `key_history` (newest first, with archive counts).
- `BackupArchive.key_version_id` (added by the startup `ALTER TABLE`): set by `run_backup` for encrypted archives; `tasks._restore_v2` decrypts with `_master_key_for_archive`.
- Settings > Encryption: Key history dropdown + Copy Selected Key.

## Traps
- Passphrases are plaintext in the DB and in the dropdown's HTML, like the existing current-key export - same trust model as before (single trusted host).
- Only NEW backups use a new key. Skip-unchanged means old files stay under the old key until they change.
- Legacy/`copy_media_file` encrypted backups do not use versions.
- Web and worker each need the `BackupKeyVersion` model (mirrored by hand).
