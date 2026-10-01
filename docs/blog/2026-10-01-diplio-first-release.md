---
title: "Diplio is out: a self-hosted media catalog and cloud backup tool, installed with one command"
date: 2026-10-01
site: SteveOmics.com
status: draft
---

# Diplio is out

A few weeks ago I wrote up the file format my backup tool uses. Today the
tool itself is public: **[Diplio](https://github.com/StevOmics/Diplio)**, a
self-hosted media cataloging and backup utility, licensed AGPL-3.0.

(You may have seen it called MediaBridge, or briefly Diploid, in earlier
posts. Diplio is the name that stuck for the public release.)

**tl;dr:** clone it, run `./setup.sh --install`, answer a handful of
questions, and you have a working instance with an empty database.

## What it does

Diplio scans folders on your server, catalogs what it finds in Postgres, and
backs it up to a Google Cloud Storage bucket as tar archives, with optional
client-side AES-256-GCM encryption and compression.

- **Libraries** are typed (movies, music, photos, documents, or a catch-all
  "files" type), and the type decides which extensions get scanned.
- **Content fingerprints** are tracked independently of path, so renaming or
  moving a file doesn't make it look new.
- **Movies** get the most attention today: NFO metadata and watch status
  synced from Jellyfin. The other types get generic cataloging and backup for
  now.
- **Backups** are skip-if-unchanged, deduplicated against what's already in
  the bucket, and verifiable. Restores rebuild a file, check its SHA-256, and
  only then move it into place, so a bad archive never overwrites a good file.
  The archive layout is documented in the [CFA spec](/cfa-spec.md).

## The part I actually want to talk about: installing it

For a long time, "installing" Diplio meant knowing a secret order of
operations: copy `.env.example`, replace four placeholder secrets by hand,
configure the media folder, generate a TLS cert, `docker compose up`, then
create an admin user. I only noticed how bad that was when I tried it on a
fresh machine and there was no install step at all.

`./setup.sh --install` now walks through all of it:

1. Checks dependencies (Docker, Compose v2, openssl, curl) and that the ports
   it needs are free, and stops before changing anything if they're not.
2. Asks where your media lives. The default is a project-local folder, and it
   warns loudly if you point it at `/`, because that folder is also the root
   the in-app folder browser is allowed to see.
3. Generates the Postgres, RabbitMQ, session and internal-API secrets.
   Existing real values are never overwritten, so re-running it won't lock
   you out of your own database.
4. Confirms the address people will use to reach it and puts it in a
   self-signed TLS certificate.
5. Asks for the admin password.
6. Builds and starts the containers.
7. Checks that every service is up and that the app answers over HTTPS.
8. Prints the URLs and next steps: add libraries, connect a bucket, set a
   backup passphrase.

It's safe to re-run. I also kept the individual flags
(`--configure-media`, `--configure-tls`, `--admin`) for people who want to do
the steps themselves.

## A fresh database really is fresh

Before release I wanted to be able to say the database starts empty and
nothing personal ships in the repo. I audited the tree for keys, tokens and
credentials, replaced my own hostname and IP in docs and UI placeholders with
example values, and stopped tracking a few generated and personal files. I
also removed the one thing that wasn't empty: a fresh install used to create
a default "Movies" library on first start. Now a new install has zero
libraries, zero files, zero backup runs, and one user, the admin you just
created.

## Testing the release, not my checkout

The most useful thing I did was test the exact tree that would be published,
from a clean copy, with no `.env`, no certs and no leftover state.

It failed. On a fresh clone, nginx started before the web app had written its
generated server config, so nginx ran with no server blocks and nothing was
reachable. My development checkout had hidden it for months because that
generated file happened to be tracked. The fix was a healthcheck on the web
service and making nginx wait for it. The same testing also showed that a
slow first boot of RabbitMQ can fail the first `docker compose up`, so the
installer now retries once.

If you publish something you run every day, test it from a clean clone before
you push. Your own machine is full of things that make it work.

## What it doesn't do yet

- Music, photos and documents get generic cataloging and backup only: no EXIF
  or ID3 extraction yet, and the catalog UI is still shaped around movies.
- Google Cloud Storage is the only backup destination.
- There's no automatic retry of a failed backup run (re-running is cheap
  because unchanged files are skipped), no CI, and no migration tool; the
  schema is upgraded additively at startup.
- Secrets such as the Jellyfin key and backup passphrase are stored in the
  database in plaintext on purpose, so unattended backups work. That's a fair
  trade on a trusted single host and a bad one elsewhere.
- I've only tested the installer on one Linux machine.

## Try it, break it

```bash
git clone git@github.com:StevOmics/Diplio.git
cd Diplio
./setup.sh --install
```

Issues and feedback are welcome. If you want to contribute code, read
`CONTRIBUTING.md` first: contributions are accepted under a grant that lets
me relicense them, which is how I'm keeping the door open to a commercial
edition later.
