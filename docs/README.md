# Documentation

- [`setup.md`](setup.md) — install and run MediaBridge (Docker Compose, `.env`, first admin user)
- [`architecture.md`](architecture.md) — services, why `celery`/`worker` are split, the backup pipeline
- [`data-model.md`](data-model.md) — database schema and how the tables relate
- [`media-types.md`](media-types.md) — how the catalog handles more than movies, and how to add a new type
- [`tls.md`](tls.md) — the nginx reverse proxy, self-signed TLS, and hardening headers
- [`TODO.md`](TODO.md) — known gaps and roadmap
- [`CHANGES.md`](CHANGES.md) — reverse-chronological log of notable changes

For AI coding assistants: see `../CLAUDE.md` for a denser, agent-oriented version of the same information plus repo-workflow specifics (branch model, publishing to the public repo).
