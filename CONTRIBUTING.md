# Contributing

Thanks for your interest in contributing!

## License and contributor agreement

This project is licensed under [AGPL-3.0](LICENSE). By submitting a pull request, you agree that:

1. You have the right to submit the contribution under AGPL-3.0.
2. You grant the project owner (Steve Ayers) a perpetual, worldwide, non-exclusive license to also relicense your contribution — including under proprietary/commercial terms — as part of MediaBridge. This keeps the option open to offer a commercial license for companies that don't want AGPL's obligations, without changing the terms this project is available under to everyone else.

If that's not acceptable for a given contribution, please open an issue to discuss before submitting a PR.

## Getting started

See `README.md` for setup instructions and `CLAUDE.md` for an architecture/data-model overview.

- Run tests: `pytest` inside `web/` and `worker/` (each has its own `requirements-dev.txt`)
- Keep `web/app/` and `worker/app/` in sync — they share a database but duplicate `models.py`/`config.py`/`db.py`/`fingerprint.py`/`gcs.py` as separate services, so schema or helper changes need to be mirrored in both.

## Pull requests

- Keep PRs focused — one change per PR.
- Describe what changed and why, not just what.
