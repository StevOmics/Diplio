# Setup

## Requirements

- Docker and Docker Compose
- A folder of media files to catalog (mounted into the containers — see below)

## Quick start

```bash
cp .env.example .env
```

Edit `.env` and fill in the placeholder values:

| Variable | Purpose |
|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Postgres credentials |
| `DATABASE_URL` | Must match the Postgres credentials above |
| `MOVIES_ROOT` | Path (inside the container) of the initial "Movies" library, e.g. `/mnt/Movies` |
| `FILESYSTEM_ROOT` | Host-side folder bind-mounted as `/mnt` inside the container, and the root the Settings browse-folder dialog is confined to — set via `./setup.sh --configure-media`, not by hand |
| `SECRET_KEY` | Signs the web session cookie |
| `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` | Celery broker credentials |
| `TERMINATOR_API_KEY` | Shared secret between `web` and the internal `terminator` service |

Generate random secrets with:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

The easiest path is the guided installer, which does everything below in order and is safe to re-run:

```bash
./setup.sh --install
```

It checks dependencies (docker, compose v2, openssl, curl, free ports), asks for the media folder, generates the Postgres/RabbitMQ/session/terminator secrets (existing real values are never overwritten), confirms the address clients will use (written to `SERVER_ADDRESS` and into the TLS cert), sets the admin password, builds and starts the containers, verifies the health endpoints, and prints the URLs and next steps. Running `./setup.sh` with no flags on a fresh clone (no `.env`) does the same.

To do the steps individually instead: configure where your media lives on the host, then create an admin user:

```bash
./setup.sh --configure-media
./setup.sh --configure-tls
docker compose up -d
./setup.sh --admin
```

`setup.sh --configure-media` prompts for the host folder to bind-mount into the container at `/mnt` (creating `.env` from `.env.example` first if it doesn't exist yet, and offering back whatever you entered last time on a re-run), writes it to `FILESYSTEM_ROOT` in `.env`, and creates the directory — this has to happen before `docker compose up`, otherwise Docker creates the missing bind-mount source itself, owned by root. Run it before the first `docker compose up -d` on a fresh clone; `docker-compose.override.yml` still defaults local/macOS dev to `./data/mediafiles` if the var is left unset.

`FILESYSTEM_ROOT` is also the root the Settings "Browse…" folder-picker is confined to (see below) — anything under it is reachable from the web UI, so pick it deliberately. The default, `./mnt_ro`, keeps exposure to a project-local folder meant to hold read-only mounts. Pointing this *host* path at something like `/mnt` (a typical Linux-server mount point for external drives — unrelated to the container's own `/mnt` mount target above, just a naming coincidence) is a reasonable choice too, but exposes whatever else lives there. Pointing it at `/` is strongly discouraged — it exposes the entire host filesystem to the browse dialog, not just your media. `setup.sh --configure-media` warns and asks for explicit confirmation if you enter `/`.

`setup.sh --admin` prompts for a password (never passed on the command line, so it never shows up in `docker compose exec` process listings) and creates or updates the given username (default `admin`).

`setup.sh --configure-tls` generates a self-signed TLS cert/key into `./certs` (gitignored) for the `nginx` reverse proxy, prompting for the hostname/IP the cert should cover — see [docs/tls.md](tls.md) for details, including the browser trust warning this causes and when to regenerate it.

## Access points

| Service | URL |
|---|---|
| Web UI | https://localhost (self-signed cert — see [docs/tls.md](tls.md)) |
| Flower (Celery monitoring) | http://localhost:5555 |
| RabbitMQ management | http://localhost:15672 |

## Mounting your media

The host folder mounted into `web`/`worker` at `/mnt` is controlled by `FILESYSTEM_ROOT` in `.env` — set it with `./setup.sh --configure-media` (see Quick start above) before the first `docker compose up -d` on a fresh clone. If left unset, `docker-compose.yml` falls back to `./mnt_ro` under the project directory. There is a single `docker-compose.yml` (no override file). Once a folder is mounted, subfolders directly under it show up in Settings as one-click "Add as storage location" suggestions, or can be picked with the Browse dialog when adding a location manually — browsing is confined to `FILESYSTEM_ROOT`, so see the security note above before pointing it at anything broader than `./mnt_ro`.

Backups go to a Google Cloud Storage bucket, configured under Settings → Cloud Storage; there is no local backup folder to mount.

## Running tests

Each service has its own virtualenv-style dependency set:

```bash
cd web && pip install -r requirements.txt -r requirements-dev.txt && pytest
cd worker && pip install -r requirements.txt -r requirements-dev.txt && pytest
```

Both suites are dependency-free of the database and broker — they test pure logic (path mapping, fingerprinting, encryption, media-type classification, etc.), not the running stack.

## Upgrading

There is no separate migration command. Pull, rebuild, and start:

```bash
git pull
docker compose up -d --build
```

The `web` service updates the database itself when it starts: it creates any new tables, adds any new columns, and runs one-time data moves (e.g. assigning existing libraries to an existing archive). Restarting `web` re-runs it safely. If a page returns "Internal Server Error" after an upgrade, look at the traceback first:

```bash
docker compose logs --tail 80 web
```

