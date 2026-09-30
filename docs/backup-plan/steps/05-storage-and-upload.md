# Step 05: Storage backend and verified upload

- **Phase:** step 5 of 9 (see `docs/backup-plan/PLAN.md`)
- **Spec sections to read:** `docs/cfa-spec.md` §6.4 (confirm size and CRC32C), §6 (the retry rule), §4 (object layout), §7 (why ranged reads exist)
- **Depends on:** 04

## Goal
All object-storage access goes through one `StorageBackend` protocol with a real GCS implementation and a local test double; `upload_and_confirm` uploads an archive and verifies what the service actually stored. Plus the four `BackupArchive` columns the run will need. **Nothing calls any of it yet** — step 6 is the first consumer.

This step was split out of the original step 5 so the orchestration in step 6 has a tested storage layer under it rather than being written against live GCS.

## Context
- `worker/app/gcs.py` (111 lines) has module-level functions taking `service_account_json` first: `upload_file`, `download_file`, `blob_exists`, `delete_blobs_with_prefix`, plus `upload_mbps_to_throttle_bytes_per_sec` and the `_ThrottledReader` bandwidth limiter. Credentials come from `CloudStorageConfig.service_account_json`.
- There is **no ranged read** anywhere today. §7 restores a clump member with one, so the protocol must have it from the start.
- There is **no test double**, and no test has ever touched a bucket. Every test in this step and the next runs against the local double; a real bucket is only touched in the final acceptance step.
- `google_crc32c` is already installed (a transitive dependency of `google-cloud-storage`). GCS reports `blob.crc32c` as **base64 of the big-endian digest**, not hex: for `b"hello world"` it is `yZRlqg==`. Verified in this environment.
- §6 ordering is load-bearing: upload, **then** confirm, **then** write the index. An archive with no index file is incomplete and restore ignores it. This step provides the upload-and-confirm half; step 6 sequences it.
- `CLOUD_UPLOAD_THROTTLE_FRACTION` must stay in sync between `web/app/main.py` and `worker/app/gcs.py` (CLAUDE.md). Do not touch either.

## Changes

### `worker/app/storage.py` (new, ~130 lines)
No imports from `app.models` or `app.db`. It may import `app.gcs`.

```python
@dataclass(frozen=True)
class ObjectStat:
    size: int
    crc32c: str          # base64, exactly as GCS reports it

class UploadVerificationError(Exception): ...

def crc32c_base64(data: bytes) -> str
def crc32c_base64_file(path: Path) -> str          # streamed, 4 MiB chunks

class StorageBackend(Protocol):
    def upload(self, key: str, local_path: Path, *, max_bytes_per_sec: float | None = None) -> None: ...
    def write_bytes(self, key: str, data: bytes, *, content_type: str = "application/json") -> None: ...
    def download(self, key: str, local_path: Path) -> None: ...
    def read_range(self, key: str, offset: int, length: int) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def stat(self, key: str) -> ObjectStat: ...

class GCSBackend:   # implements the protocol over app.gcs + google-cloud-storage
    def __init__(self, service_account_json: str, bucket_name: str, project_id: str | None = None)

def upload_and_confirm(backend, key: str, local_path: Path, *,
                       attempts: int = 5, sleep=time.sleep,
                       max_bytes_per_sec: float | None = None) -> ObjectStat
```

- `crc32c_base64_file` streams in 4 MiB chunks, matching `fingerprint.HASH_CHUNK_SIZE`'s reasoning — never read a multi-GB archive into memory.
- `GCSBackend.read_range` uses `blob.download_as_bytes(start=offset, end=offset + length - 1)`. **GCS's `end` is inclusive**; an off-by-one here surfaces much later as a one-byte-short restore.
- `GCSBackend.stat` must `blob.reload()` before reading `blob.size` / `blob.crc32c`, so the values come from the service rather than a stale local object.
- `GCSBackend.delete` treats a missing object as success (idempotent), so step 6's cleanup and any later retry are safe to repeat.
- **`upload_and_confirm`** implements §6.4 plus §6's retry rule:
  1. Compute the local file's size and `crc32c_base64_file` **once**, before the loop.
  2. Up to `attempts` times (default 5): `backend.upload(...)`, then `backend.stat(key)`. If size and crc32c both match, return the `ObjectStat`.
  3. On a mismatch or an exception, sleep with exponential backoff plus jitter (`sleep(2 ** i + random.uniform(0, 1))`) and retry. `sleep` is a parameter so tests inject a no-op instead of waiting.
  4. After the last attempt raise `UploadVerificationError` naming the key, the expected size/crc32c, and what was actually found.
- A verification mismatch must **not** be treated as success under any circumstance. This is the only thing standing between a corrupted upload and an index file that claims it is good.

### `worker/tests/storage_double.py` (new, ~60 lines)
Test-only, lives under `tests/` so it can never be configured in production.

- `LocalBackend(root: Path)` — implements the protocol against files under `root`, creating parent directories on write, computing crc32c with the same helper as production, and honouring `read_range` by seeking. `read_range` past end-of-object raises rather than silently returning short data.
- `FlakyBackend(inner, *, fail_uploads_on: set[int], exc: Exception)` — counts `upload` calls and raises on the listed 1-based call numbers, delegating everything else. Used for the retry tests.
- `CorruptingBackend(inner)` — writes one flipped byte, so `upload_and_confirm` sees a genuine crc32c mismatch rather than a simulated one.

### `web/app/models.py` and `worker/app/models.py` (mirror both)
On `BackupArchive`:
- `archive_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)` — the uuid4 from `PackedArchive`
- `archive_type: Mapped[Optional[str]] = mapped_column(String)` — `"clump"` / `"single"` / `"part"`
- `crc32c: Mapped[Optional[str]] = mapped_column(String)` — base64, as confirmed at upload
- `indexed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))` — set only after the index object is written

All nullable. `NULL` on all four means a pre-v2 archive, which the legacy restore path handles; comment that. `indexed_at IS NULL` is how step 7's restore identifies an incomplete archive (§6: "An archive with no index file is incomplete and is ignored by restore").

### `web/app/main.py`
Four more guards in the step-01/02 block in `on_startup`:
```python
db.execute(text("ALTER TABLE backup_archives ADD COLUMN IF NOT EXISTS archive_id VARCHAR(36)"))
db.execute(text("ALTER TABLE backup_archives ADD COLUMN IF NOT EXISTS archive_type VARCHAR"))
db.execute(text("ALTER TABLE backup_archives ADD COLUMN IF NOT EXISTS crc32c VARCHAR"))
db.execute(text("ALTER TABLE backup_archives ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ"))
db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_backup_archives_archive_id ON backup_archives (archive_id)"))
db.commit()
```
The index is unique and the column nullable — Postgres permits many `NULL`s in a unique index, so every legacy row coexists while any v2 `archive_id` is forced distinct.

## Do Not Touch
- Every existing function in `worker/app/gcs.py`, including `delete_blobs_with_prefix`. The legacy path calls them and step 07 tests it. `GCSBackend` wraps them or the client directly; it does not rewrite them.
- `_ThrottledReader`, `upload_mbps_to_throttle_bytes_per_sec`, and `CLOUD_UPLOAD_THROTTLE_FRACTION`.
- `worker/app/packer.py`, `worker/app/backup_index.py`, `worker/app/tasks.py`, `worker/app/encryption.py`, `web/app/catalog.py`.
- Any route or template. This step adds no UI.

## Tests to Write First
`worker/tests/test_storage.py` (new, pure logic + `tmp_path`; no bucket, no database).

CRC32C:
- `test_crc32c_base64_known_vector`: `b"hello world"` → `"yZRlqg=="`
- `test_crc32c_base64_empty`: `b""` → the value for an empty digest (derive it, then hard-code it)
- `test_crc32c_file_matches_bytes`: `crc32c_base64_file` of a 10 MiB random file equals `crc32c_base64` of the same bytes — proves the chunk loop accumulates

`LocalBackend`:
- `test_upload_download_roundtrip`
- `test_write_bytes_then_download`
- `test_read_range_matches_slices`: several offsets and lengths, including length 0 and the final byte
- `test_read_range_past_end_raises`
- `test_stat_reports_size_and_crc32c`
- `test_delete_is_idempotent`: deleting a missing key succeeds
- `test_exists`

`upload_and_confirm`:
- `test_confirms_and_returns_stat`: one upload call, returned `ObjectStat` matches the local file
- `test_retries_then_succeeds`: `FlakyBackend(fail_uploads_on={1, 2})` → succeeds on the third call
- `test_exhausts_attempts_and_raises`: `FlakyBackend` failing all 5 → `UploadVerificationError`, and the backend saw exactly 5 upload calls
- `test_crc_mismatch_is_not_treated_as_success`: `CorruptingBackend` → raises `UploadVerificationError`, never returns
- `test_error_message_names_key_and_both_checksums`
- `test_backoff_sleeps_between_attempts`: inject a recording `sleep`; assert it was called `attempts - 1` times with strictly increasing base delays
- `test_local_checksum_computed_once`: monkeypatch/count `crc32c_base64_file` and assert one call across a retrying upload — re-reading a multi-GB file per attempt is the performance trap here

`web/tests/test_models_sync.py` — add:
- `test_backup_archive_v2_columns_mirrored`: `assert_columns_mirrored("backup_archives", ["archive_id", "archive_type", "crc32c", "indexed_at"])`

## Commands
- Run: `cd worker && pytest -x -q tests/test_storage.py`
- Full suites: `cd worker && pytest -x -q` then `cd web && pytest -x -q`
- Guards against a live database: `docker compose build web && docker compose up -d db web && docker compose logs --tail 20 web`. **The build is required** — `web` has `build: ./web` and no source bind-mount.
- Columns: `docker compose exec -T db psql -U mediabridge -d mediabridge -c '\d backup_archives'`

## Done When
- [ ] All listed tests exist and pass
- [ ] Both full suites pass with no services running
- [ ] `backup_archives` has the four columns and a unique index on `archive_id`
- [ ] A second container start is a no-op
- [ ] No test contacts a real bucket; `LocalBackend` and friends live under `tests/`
- [ ] `git diff` shows no change to existing `gcs.py` functions, the packer, the index builder, or any task
- [ ] A row added at the top of `docs/CHANGES.md` (`260917 | ...`)

## Notes for the Implementer
- **The point of this step is that a corrupted upload can never be mistaken for a good one.** If a test forces you to choose between "make it pass" and "keep the verification strict", keep the verification strict and report the problem.
- GCS's ranged-read `end` is **inclusive**. Write the `read_range` boundary tests first and let them tell you if you got it wrong.
- Compute the local checksum once, outside the retry loop. There is a test for it because the obvious implementation re-reads the whole archive on every attempt.
- `random.uniform` in the backoff makes the delay non-deterministic; that is why `sleep` is injectable. Do not seed `random` globally to work around it.
- `google_crc32c.Checksum().digest()` returns big-endian bytes; base64 them. Do not hex-encode — GCS will never match.
- Do not add `archive_type = "legacy"` backfill. `NULL` already means pre-v2, and touching existing rows is out of scope for this step.
- `StorageBackend` is a `typing.Protocol`, not an ABC — no inheritance, so `LocalBackend` stays independent of production code.
