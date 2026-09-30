# TODO / Roadmap

## Media types (see media-types.md)

- [ ] EXIF metadata extraction for photos (date taken, camera, GPS)
- [ ] ID3/tag metadata extraction for audio (artist, album, track)
- [ ] Sidecar/junk filtering for `"files"` locations (e.g. don't catalog `poster.jpg`/`fanart.jpg` as standalone photos)
- [ ] Type-aware catalog UI (hide movie-only columns like rating/watched for non-movie rows; per-type detail views)

## UI

- [x] For the Catalog view, we should follow the general conventions of Windows file explorer, with multiple views (List, Thumbnails) and adapt this to the file type (Movies, Pictures).
- [x] For media/movies the catalog should display the file name. the full path isn't necessary. (full path moved to a hover tooltip, row label is now just the title/filename)
- [x] For Files generally, we should have a hierarchical view that shows the file name and path. Ideally, with collapsible folders. So if I add Documents, it should show the folders under that Finance/ Family/ Health/ and the files exercize.xlsx, etc. Then if I click Finance, it should take me into Finance and show its content. (one level at a time via breadcrumb + folder chips, not collapsible in place - see follow-up below)
- [ ] Catalog's folder browser (`_folder_view` in `main.py`) navigates one directory at a time (breadcrumb + subfolder chips), not an in-place collapsible tree; it also rescans every row in the selected library on each request rather than an indexed lookup, fine at catalog sizes seen so far but worth revisiting if a "files" library gets very large
- [ ] The browse-to-folder dialog (`/settings/browse-folders`) only wired up on the "Add a storage location" form — the per-row edit path inputs on the same page still require typing a path manually

## Backup rework (`docs/cfa-spec.md` v2.0)

All nine steps in `docs/backup-plan/PLAN.md` are done - backup run, restore, and skip-unchanged are implemented and pass the spec's §9 acceptance tests end to end. What's left is deliberately-deferred follow-on work, not unfinished plan steps:

- [ ] `worker/app/encryption.py`'s plaintext manifest is a truncation vector: `chunk_count` is unauthenticated, so a shortened restore passes silently. Encryption changes are out of scope for v2.0 (spec §8), so this stays open
- [ ] `docs/cfa-spec.md` §3's table and §9 contradict each other: the table puts the single/part boundary at `max_size`, but tar overhead then pushes the object past `max_size`, which §9 forbids. Resolved in code by `packer.payload_ceiling` (one margin for all three archive types); **the spec text still says `max_size`** and should be corrected at the next spec revision
- [ ] `worker/app/models.py`'s `MediaFile` is still missing columns the real table has `NOT NULL` with no server-side default (`extension`/`media_type`/`watched`/`play_count`) - an `IntegrityError` waiting to happen the day worker code inserts one. (`CloudStorageConfig`'s `provider`/`connected`/`is_backup_target` were mirrored 2026-09-19 after a test teardown failed to restore the real row.)
- [ ] Restore's §7 index-file fallback ("look up the hash ... in the index files when the database is unavailable") is not implemented - only the database lookup path exists (`_v2_ledger_parts` in `worker/app/tasks.py`, step 7). A disaster-recovery feature needing its own design (bucket object listing, a search bound as the number of archives grows); see `DEVIATIONS.md` D7
- [ ] Archives follow-ups: archive subfolders only work for cloud archives; changing a library's subfolder (or archive) doesn't re-upload files already backed up at that archive - skip-unchanged is keyed by (file, archive), so only new/changed files land in the new folder (restore still works, records hold full object keys); library backups to a _local_ archive still use the legacy per-file `CopyJob` pipeline (not tracked under Backup Runs); the old single-bucket `CloudStorageConfig.bucket_name`/`prefix` are still used by the speed test and legacy path; dead routes to delete once nothing links them: `set-backup`, `unset-backup`, `backup-local`, `backup-cloud` (library), `/settings/cloud-storage/set-backup|unset-backup`; ~20 routes still redirect errors to `/?error=` (lands on System Status, not the catalog); no UI to move a library's existing backups between archives
- [ ] Key history follow-ups: no way to delete/annotate a version; an archive-level key version isn't in the plaintext index (a disaster-recovery tool must try versions or use the database); files unchanged since a rotation are not re-encrypted under the new key (a forced re-encrypt run doesn't exist)
- [ ] **Encryption (D9) follow-ups:** rewrite `docs/cfa-spec.md` §3/§5/§7/§8 and retire D5 (Steve: do this at the end); the index still holds plaintext sizes/mtimes (accepted, bucket is private) - hashes are no longer plaintext for encrypted runs as of D14 (keyed `content_id` instead), plain runs are unaffected; no key id in the index (key versions are tracked in the database only); a disaster-recovery tool that decrypts `paths_enc` (and, for a content-id-keyed index, `sha256_enc`) and restores from the bucket without the database (ties into D7); encrypted runs need ~2x temp disk (blobs + tars); make encryption mandatory for v2 if desired (today off = plain tars)
- [ ] **Content-id (D14) follow-up:** dedup only recognizes previously-archived content under a key version once `BackupRecordContentId` has been backfilled for it (automatic on rotation and once at deploy time, but not retroactive for a version that predates both) - low-risk (falls back to "needs a fresh upload," not silent data loss) but worth a manual backfill trigger if it ever needs re-running by hand

## Reliability

## Infrastructure

- [ ] `nginx/nginx.conf`'s `client_max_body_size` is left unlimited (`0`) — tighten once it's confirmed whether any web-facing route streams large file bodies through `web` directly (see `docs/tls.md`)
- [ ] No automated TLS cert renewal — `./setup.sh --configure-tls` cert is long-lived (~825 days) and manual to regenerate; custom uploaded certs (Settings > TLS / Domain) are the same, no ACME/auto-renewal
- [ ] CI pipeline (tests currently only run locally/manually)
- [ ] A real migration tool (schema changes are hand-written `ALTER TABLE ... IF NOT EXISTS` guards today — see `architecture.md#no-migration-tool`). The backup rework needs only additive nullable columns, so it does not force this
- [ ] `ruff` and `mypy` are allowed in `.claude/settings.json` but are in neither `requirements-dev.txt` and have no config

## Open source

- [ ] CLA enforcement (currently just a clause in CONTRIBUTING.md, not automated on PRs)

- [ ] Compression follow-ups: per-library setting (documents compress, movies don't - today it is one global toggle); a "re-pack existing backups with compression" action (toggling on only affects new/changed files); zstd as a second format (the per-file `compression` field already allows it); the catalog only marks compressed files `(gz)`, it doesn't show space saved
- [ ] `docs/cfa-spec.md` rewrite list now also includes D10 (compression: §3 `.gz` member names, §5 `compression` field, §7 gunzip step)
- [ ] Verify: sampled verify's encrypted head/tail read is a whole chunk (up to 64 MiB each), and it can't see a corrupt middle that keeps the object's CRC32C (use Deep verify)
- [ ] Sync/verify follow-ups (step 16): a copy adopted from existing bytes is not added to the archive's `index/<id>.json` (D11) - an optional "refresh indexes" pass could write them; skip-unchanged compares against the catalog's size, not the size at backup time; `_record_archive` does one SELECT per file, fine for incremental runs but slow for a first backup of 100k+ files; `catalog.scan_library` queries per file; legacy (pre-v2) records are re-uploaded once in v2 format

- [ ] Portability (step 18, `DEVIATIONS.md` D13) follow-ups: option B ("pick a different folder") repoints the whole library's local path - no per-file path remapping, declined as unnecessary for the fresh-install scenario; the Libraries page's "Ignore for now" dismiss is session-local only (reappears next page load) - a persistent per-library "permanently silence this mismatch" state would need new schema
- [ ] No automatic retry of a failed backup or restore run (re-run it; skip-unchanged makes a backup resume cheaply). Backup/Restore Runs now have a Cancel button (queued/running only), but a cancelled run's temp directory isn't guaranteed cleaned up (SIGTERM doesn't run Python's context-manager exit)
