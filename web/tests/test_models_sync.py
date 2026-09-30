from tests.model_sync import assert_columns_mirrored


def test_transfer_config_sizes_mirrored():
    assert_columns_mirrored("transfer_config", ["min_size_bytes", "clump_size_bytes", "max_size_bytes"])


def test_cloud_storage_prefix_mirrored():
    assert_columns_mirrored("cloud_storage_config", ["prefix"])


def test_storage_location_excludes_mirrored():
    assert_columns_mirrored("storage_locations", ["exclude_globs"])


def test_backup_record_hash_columns_mirrored():
    assert_columns_mirrored("backup_records", ["sha256", "mtime_ns"])


def test_backup_archive_v2_columns_mirrored():
    assert_columns_mirrored("backup_archives", ["archive_id", "archive_type", "crc32c", "indexed_at"])


def test_media_file_not_null_columns_mirrored():
    # worker/app/sync_run.py is the first worker code that INSERTs a fresh
    # MediaFile row - without these, that violates the real table's
    # NOT NULL constraints (see worker/app/models.py's comment on MediaFile).
    assert_columns_mirrored("media_files", ["extension", "media_type", "watched", "play_count"])


def test_sync_run_mirrored():
    assert_columns_mirrored(
        "sync_runs",
        [
            "library_storage_location_id", "archive_storage_location_id", "status", "files_total", "files_synced",
            "files_failed", "files_skipped", "bytes_synced", "detail", "error_message", "results_json", "phase",
            "phase_done", "phase_total", "phase_started_at", "heartbeat_at", "celery_task_id",
        ],
    )
