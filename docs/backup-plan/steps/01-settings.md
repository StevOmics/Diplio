# Step 01: Settings for the v2 backup pipeline

- **Phase:** step 1 of 8 (see `docs/backup-plan/PLAN.md`)
- **Spec sections to read:** `docs/cfa-spec.md` §2 (Settings), §3 (Packing — for why sizes are bytes)
- **Depends on:** nothing

## Goal
The three packing sizes from spec §2, the GCS object prefix, and per-source exclude globs exist as configuration in both model files, are editable in the Settings UI, and are validated on save. **Nothing reads them yet** — step 3 is the first consumer.

## Context
- `docs/cfa-spec.md` §2 defines `min_size` (2.5 MiB), `clump_size` (64 MiB), `max_size` (1 GiB), `bucket`/`prefix`, and `sources` (paths plus exclude globs).
- `TransferConfig` (`web/app/models.py:212`) already holds sizing knobs, but on a **percentage** model: `max_size_gb` + `split_over_percent`, `min_size_mb` + `clump_under_percent`. Those drive today's pipeline via `_plan_backup` (`web/app/main.py:1101`) and must keep working — PLAN.md step 7 retires them. The new columns live alongside them.
- `CloudStorageConfig` (`web/app/models.py:238`) has `bucket_name` but no prefix.
- `StorageLocation` (`web/app/models.py:11`) is already the spec's "source": it has the path. Only exclude globs are missing.
- The repo has no migration tool. Additive columns need an idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` guard in `on_startup` (`web/app/main.py:70-81`), next to the existing `media_type` one.
- Storage location rows are edited through a per-row hidden form (`settings.html:51-53`) posting to `/settings/storage-locations/{id}/update` (`web/app/main.py:547`). Adding a field means adding a cell bound to that same form plus a `Form(...)` parameter — no new route.
- `web/tests/model_sync.py` already exists and is the fixture for the web/worker mirror check. Use `assert_columns_mirrored(table, columns)`; do **not** assert `compare_model(...).is_clean`, because the worker deliberately mirrors only a subset of columns and that would fail on pre-existing, intentional differences.

## Changes

### `web/app/backup_settings.py` (new, ~40 lines)
Two pure helpers, no imports from `app.models` or `app.db`:

- `normalize_prefix(raw: str | None) -> str | None` — strip whitespace, strip leading `/`, collapse to `None` when the result is empty or was only slashes, otherwise guarantee exactly one trailing `/`.
- `parse_exclude_globs(raw: str | None) -> list[str]` — split on newlines, strip each line, drop blanks, preserve order, de-duplicate.
- `MIB = 1024 * 1024` and `GIB = 1024 ** 3` constants used by the defaults and the form conversion.

### `web/app/models.py` and `worker/app/models.py` (mirror both)
`TransferConfig` — three new columns, **bytes** in `BigInteger`:
- `min_size_bytes: Mapped[int] = mapped_column(BigInteger, default=2621440)` (2.5 MiB)
- `clump_size_bytes: Mapped[int] = mapped_column(BigInteger, default=67108864)` (64 MiB)
- `max_size_bytes: Mapped[int] = mapped_column(BigInteger, default=1073741824)` (1 GiB)

Bytes rather than the neighbouring `float` MiB/GiB columns because step 3 computes `k = ceil(size / (max_size - 1 MiB))` and float rounding there changes part counts. Add a comment saying so, and a comment on the old percentage columns marking them legacy-until-step-7.

`CloudStorageConfig` — `prefix: Mapped[Optional[str]] = mapped_column(String)`, commented as the spec §4 object-key prefix, e.g. `server01/`, null meaning bucket root.

`StorageLocation` — `exclude_globs: Mapped[Optional[str]] = mapped_column(Text)`, commented as newline-separated glob patterns, null meaning no exclusions.

`worker/app/models.py` needs `BigInteger` and `Text` in its imports if not already present.

### `web/app/main.py`
In `on_startup`, after the existing `media_type` guard:
```python
db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS min_size_bytes BIGINT NOT NULL DEFAULT 2621440"))
db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS clump_size_bytes BIGINT NOT NULL DEFAULT 67108864"))
db.execute(text("ALTER TABLE transfer_config ADD COLUMN IF NOT EXISTS max_size_bytes BIGINT NOT NULL DEFAULT 1073741824"))
db.execute(text("ALTER TABLE cloud_storage_config ADD COLUMN IF NOT EXISTS prefix VARCHAR"))
db.execute(text("ALTER TABLE storage_locations ADD COLUMN IF NOT EXISTS exclude_globs TEXT"))
db.commit()
```

New route `POST /settings/backup-archiving`, following the shape of `save_transfer_config` (`web/app/main.py:670`) — form fields in **MiB** for legibility, converted to bytes on save:
```python
@app.post("/settings/backup-archiving", dependencies=[Depends(require_login)])
def save_backup_archiving_config(
    min_size_mib: float = Form(...),
    clump_size_mib: float = Form(...),
    max_size_mib: float = Form(...),
    prefix: str = Form(""),
    db: Session = Depends(get_db),
):
```
Validation, each returning `RedirectResponse(url="/settings?error=...", status_code=303)` in the existing style:
1. all three sizes `> 0` → `Archive+sizes+must+be+positive`
2. `min_size_bytes < clump_size_bytes` → `Min+size+must+be+smaller+than+the+clump+size`
3. `clump_size_bytes <= max_size_bytes` → `Clump+size+cannot+exceed+the+max+object+size`
4. `max_size_bytes > MIB` → `Max+object+size+must+be+larger+than+1+MiB` — the split margin in spec §3 is `max_size - 1 MiB`, which must stay positive

On success write the three byte values plus `normalize_prefix(prefix)` onto the singleton `CloudStorageConfig`/`TransferConfig` rows (creating them if absent, as `save_transfer_config` does), commit, and redirect to `/settings?flash_status=ok&flash_message=Backup+archiving+settings+saved`.

Extend `update_storage_location` (`web/app/main.py:548`) with `exclude_globs: str = Form("")`, stored as `"\n".join(parse_exclude_globs(exclude_globs)) or None`.

The `/settings` GET handler (`web/app/main.py:393`) already passes `transfer_config`; make sure `cloud_config` is available to the template for the prefix field, following whatever it already does for the cloud storage section.

### `web/app/templates/settings.html`
- A new `<h2>Backup archiving</h2>` section immediately **after** `Transfer settings` (line 399) and before `Error handling & retries` (line 454), with a `<form class="toolbar" method="post" action="/settings/backup-archiving">` containing the three MiB number inputs (`step="0.1"`, `min="0"`) pre-filled from the stored bytes divided by `MIB`, a text input for `prefix`, and a Save button. A short `<p class="meta">` explaining that files under min size are clumped, files over max size are split, and that these replace the percentage-based Transfer settings above once the new pipeline lands.
- In the storage locations table (line 18-50): a new `Excludes` column header and a cell per row with `<textarea name="exclude_globs" form="edit-loc-{{ entry.location.id }}" rows="2" placeholder="**/.cache/**">{{ entry.location.exclude_globs or "" }}</textarea>`. Bump the empty-state `colspan="6"` to `7`.

## Do Not Touch
- `max_size_gb`, `split_over_percent`, `min_size_mb`, `clump_under_percent`, `clump_split_enabled` — values, meaning, and the `/settings/transfer` route and form all stay exactly as they are.
- `_plan_backup` (`web/app/main.py:1101`) and every worker task. No behaviour change to the running backup path.
- `worker/app/encryption.py`, `worker/app/gcs.py`, `worker/app/tasks.py`.
- `web/tests/model_sync.py` — it is already written and verified; use it, don't edit it.

## Tests to Write First
`web/tests/test_backup_settings.py` (new, pure logic — no database, no services):
- `test_normalize_prefix_adds_trailing_slash`: `"server01"` → `"server01/"`
- `test_normalize_prefix_strips_leading_slash`: `"/server01/"` → `"server01/"`
- `test_normalize_prefix_idempotent`: `"server01/"` → `"server01/"`
- `test_normalize_prefix_empty_is_none`: `""`, `"   "`, `"/"`, and `None` all → `None`
- `test_normalize_prefix_keeps_nested`: `"/a/b"` → `"a/b/"`
- `test_parse_exclude_globs_strips_and_drops_blanks`: `"  **/.cache/**  \n\n*.part\n"` → `["**/.cache/**", "*.part"]`
- `test_parse_exclude_globs_dedupes_preserving_order`: `"a\nb\na"` → `["a", "b"]`
- `test_parse_exclude_globs_empty`: `""` and `None` → `[]`

`web/tests/test_backup_settings_validation.py` (new) — validate the four rules against the helper you extract for them, or via `TestClient` if that is already how routes are tested in this repo (check `web/tests/test_auth.py` first and follow it):
- `test_rejects_non_positive_size`
- `test_rejects_min_not_below_clump`: min 64 MiB, clump 64 MiB → rejected
- `test_rejects_clump_above_max`: clump 2 GiB, max 1 GiB → rejected
- `test_rejects_max_at_or_below_one_mib`: max 0.5 MiB → rejected
- `test_accepts_spec_defaults`: 2.5 / 64 / 1024 MiB → accepted, and converts to exactly 2621440 / 67108864 / 1073741824 bytes

`web/tests/test_models_sync.py` (new):
- `test_transfer_config_sizes_mirrored`: `assert_columns_mirrored("transfer_config", ["min_size_bytes", "clump_size_bytes", "max_size_bytes"])`
- `test_cloud_storage_prefix_mirrored`: `assert_columns_mirrored("cloud_storage_config", ["prefix"])`
- `test_storage_location_excludes_mirrored`: `assert_columns_mirrored("storage_locations", ["exclude_globs"])`

## Commands
- Run: `cd web && pytest -x -q tests/test_backup_settings.py tests/test_backup_settings_validation.py tests/test_models_sync.py`
- Full suites: `cd web && pytest -x -q` then `cd worker && pytest -x -q`
- Bring it up to confirm the guards run: `docker compose build web && docker compose up -d db web && docker compose logs --tail 20 web`
  — the **build** is required. `web` has `build: ./web` and no source bind-mount, so `up -d` alone reruns the old image and the new `ALTER TABLE` guards never execute.

## Done When
- [ ] All listed tests exist and pass
- [ ] Both full suites pass, with no services running for the default suite
- [ ] `docker compose up -d web` starts cleanly and the five columns exist (`docker compose exec db psql -U mediabridge -d mediabridge -c '\d transfer_config'`)
- [ ] Starting the container a second time is a no-op — the guards are idempotent
- [ ] The Settings page renders the new section and the Excludes column, and saving each form round-trips the values
- [ ] `git diff` shows no change to `_plan_backup`, the `/settings/transfer` route, or any worker task
- [ ] A row added to `docs/CHANGES.md` (top, `260917 | ...`) describing the new settings

## Notes for the Implementer
- **The point of this step is that nothing consumes the new settings.** If you find yourself editing `_plan_backup` or a worker task to use them, stop — that is step 3 and later.
- Store bytes, display MiB. Convert at the form boundary only, and use `int(round(value * MIB))` so 2.5 MiB lands on exactly 2621440 rather than 2621439.
- The `ALTER TABLE` guards use `NOT NULL DEFAULT` for the three size columns so existing rows get the spec defaults, but `prefix` and `exclude_globs` are plain nullable — null is a meaningful value for both (bucket root, no exclusions).
- Order the validation checks as listed. Check positivity first, or the later comparisons produce a confusing error for a negative input.
- Check `web/tests/test_auth.py` before writing the validation tests — if the repo has no `TestClient` pattern yet, extract the four rules into a `validate_archive_sizes(...) -> str | None` helper in `backup_settings.py` returning an error message or `None`, and unit-test that instead. Do not add `httpx`/`TestClient` to `requirements-dev.txt` just for this step.
- `docs/cfa-spec.md` §2 notes "Use 10 MiB for Nearline" for `min_size`. That is a deployment choice, not a second default — ship 2.5 MiB and mention Nearline in the form's help text.
