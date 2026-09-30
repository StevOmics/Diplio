# Step 15: Live run progress

- **Depends on:** 11 (run tracking). Display and reporting only - no change to what a run does.

## Why
A large upload sat on "Uploading 0/2" for minutes with nothing to say whether it was working. Now the Backup Runs table shows a bar, a rate, an ETA and a heartbeat.

## What the worker reports (`BackupRun`, written at most ~once a second)
`phase` (`hashing` -> `preparing` (only if compressing/encrypting) -> `packing` -> `uploading`), `phase_done` / `phase_total` (files for hashing/preparing, **bytes** for uploading, total 0 = unknown), `phase_started_at`, `heartbeat_at`. `detail` also says "Checksumming n/N" then "Uploading n/N" per archive (`upload_and_confirm` checksums the whole archive before sending, a visible pause on big archives). All cleared when the run ends.

Byte progress comes from `gcs._ProgressReader` around the upload stream: position-based (`tell()`), so a retried chunk shows honestly rather than double-counting; granularity is the uploader's 8 MiB chunk. It is plumbed as an optional `progress_cb` through `gcs.upload_file` -> `GCSBackend.upload` -> `upload_and_confirm`. `backup_run._Progress` is the throttled writer; it never raises and does nothing without a `run_id`.

## What the web app shows
`GET /copy-jobs/runs/status` returns `_run_progress_view` for the latest 25 runs: percent, rate and ETA (measured over the current phase, hidden for the first 3 s), `heartbeat_age`, and `stalled` when a running run hasn't reported for `RUN_STALL_SECONDS` (120 s). `copy_jobs.html` polls it every 2 s and updates in place (no more full-page reload); it reloads once when a run finishes. The spinner is blue while beating, amber if the last update is over 30 s old, and stops and turns red when stalled, with a "may be stuck, check the worker logs" note. Packing has no known total, so it shows an indeterminate bar.

## Limits
- Hashing and preparing advance per *file*, so one enormous file is a single long step with no intermediate updates; the heartbeat threshold is generous for that reason.
- The ETA covers the current phase only, not the whole run.
- Only cloud (v2) runs are covered. Legacy `CopyJob` rows already had their own progress columns.
- Runs from before this step have no phase data and show as before.

## Tests
`worker/tests/test_run_progress.py` (reader, `upload_and_confirm` callbacks and retry, `_Progress` throttling, and a DB-backed run asserting the phase sequence and clean end state); `web/tests/test_run_progress_view.py` (percent/rate/ETA/stall logic).
