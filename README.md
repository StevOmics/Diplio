# MediaBridge
Media Archiving Utility

# Core functions
MediaBridge catalogs and backs up files from one or more local folders ("storage locations"). Each location is typed
(movies, music, photos, documents, or files as a misc catch-all) which decides what it scans for — see [`docs/media-types.md`](docs/media-types.md).
Cataloged files can be backed up to a local target and/or Google Cloud Storage, with optional client-side encryption,
and later restored or verified against the original.

# Architecture
This project follows a microservice architecture, supporting multiple container-based services to manage specific concerns:

web: FastAPI + Jinja frontend to support user interface
worker: Celery workers that do the actual file copy/backup/restore/verify work
database: postgres container to support application as well as media catalog functions

See [`docs/`](docs/) for setup instructions and a full architecture/data-model breakdown.

# License
Copyright (c) 2026 Steve Ayers.

Licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0). This means you're free to use, modify, and self-host MediaBridge, but if you run a modified version as a network service, you must make your modified source available to its users.

A separate commercial license (for embedding MediaBridge in a closed-source or SaaS product without AGPL's source-sharing obligations) may be made available in the future — contact the project owner if interested.

