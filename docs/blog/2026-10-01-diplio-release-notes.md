---
title: "Diplio first public release: release notes"
date: 2026-10-01
site: SteveOmics.com
status: draft
---

# Diplio first public release: release notes

Diplio is a self-hosted media cataloging and backup utility, licensed
AGPL-3.0. This is its first public release. Repository:
[StevOmics/Diplio](https://github.com/StevOmics/Diplio).

## Highlights

- **Guided installer:** `./setup.sh --install` handles first-time setup end to end.
- **Catalog:** typed libraries (movies, music, photos, documents, and a
  catch-all "files" type), cataloged in Postgres with content fingerprints
  that are independent of path.
- **Jellyfin sync:** watched and play-count data for one configured user, plus
  NFO metadata for movies.
- **Cloud backup:** tar archives in Google Cloud Storage, with optional
  client-side AES-256-GCM encryption and gzip compression.
- **Restore and verify:** restores check the SHA-256 before moving a file into
  place; verify is shallow by default, with an optional deep re-hash.
- **Web UI:** FastAPI and Jinja templates for catalog browsing, settings, and
  backup and restore status.

## Install

```bash
git clone git@github.com:StevOmics/Diplio.git
cd Diplio
./setup.sh --install
```

The installer checks dependencies (Docker, Compose v2, openssl, curl) and
ports, asks for your media folder, generates secrets, creates a self-signed
TLS certificate for the address you confirm, sets the admin password, starts
the containers and checks that they respond. It is safe to re-run and never
overwrites existing real secrets. The individual steps are also available as
`--configure-media`, `--configure-tls` and `--admin`.

After install: add libraries, connect a bucket, and set a backup passphrase.

## Backups

- Unchanged files (same size and mtime) are skipped without hashing or any
  bucket requests.
- Files whose content is already in the archive are recorded against the
  existing bytes instead of being uploaded again.
- Small files are clumped into shared archives and oversized files are split.
  Each archive gets a JSON index, written only after the upload is confirmed
  by size and CRC32C.
- A restore that fails leaves the existing file untouched, and one failing
  file doesn't stop the rest of the run.
- The archive layout follows the [CFA spec](/cfa-spec.md).

## Fixes made while preparing this release

- Fixed a fresh-clone startup race: nginx could start before the web app had
  written its generated server config and then serve nothing. The web service
  now has a `/health` healthcheck and nginx waits for it.
- The installer retries `docker compose up` once, because a slow first boot
  of RabbitMQ can fail the first attempt.
- A fresh install no longer creates a default "Movies" library. It starts
  with no libraries, files or backup runs, and only the admin user.
- Removed personal hostnames and IPs from docs and UI placeholders, untracked
  generated files, and removed the idle `beat` service.
- Merged `docker-compose.override.yml` into a single `docker-compose.yml`.
  The web service no longer hot-reloads; use `docker compose up -d --build web`
  or `./dev.sh restart` after code changes.

## Known limitations

- Music, photos and documents get generic cataloging and backup only: no EXIF
  or ID3 extraction, and the catalog UI is still shaped around movies.
- Google Cloud Storage is the only backup destination.
- A failed backup run is not retried automatically; re-run it, and unchanged
  files are skipped.
- No CI and no migration tool. The schema is upgraded additively at startup,
  so renames, drops and type changes need a hand-written migration.
- Secrets (Jellyfin API key, backup passphrase, service account JSON) are
  stored in plaintext in Postgres by design, so unattended backups work. That
  suits a trusted single host and is a poor fit elsewhere.
- The installer has been tested on one Linux machine only. Other operating
  systems and Compose versions are untested.

## Contributing

Issues and feedback are welcome. Read `CONTRIBUTING.md` before sending code:
contributions are accepted under a grant that lets the project owner
relicense them.
