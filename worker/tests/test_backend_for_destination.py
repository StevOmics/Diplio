"""Tests for storage.backend_for_destination - the single dispatch point that
builds a GCSBackend or S3Backend from an archive StorageLocation's path
(s3:// vs gs:///gcs://), shared by backup_run.py, restore_run.py, tasks.py's
verify, and sync_run.py. DB-backed (MEDIABRIDGE_TEST_DB=1) since it queries
CloudStorageConfig / S3StorageConfig - see tests/conftest.py's docstring.
"""
import pytest

from app import gcs
from app.storage import GCSBackend, S3Backend, StorageConfigError, backend_for_destination
from tests.conftest import set_cloud_config


class _FakeGCSClient:
    def bucket(self, name):
        return name


@pytest.fixture(autouse=True)
def _fake_gcs_client(monkeypatch):
    # backend_for_destination's GCS branch builds a real GCSBackend, whose
    # __init__ calls gcs._client(...) to authenticate - stub it out so a fake
    # service_account_json in config doesn't need to parse as real credentials.
    monkeypatch.setattr(gcs, "_client", lambda *a, **k: _FakeGCSClient())


def test_gcs_destination_builds_a_gcs_backend(db_session, catalog, cloud_storage_config_state):
    set_cloud_config(db_session, bucket_name="fallback-bucket")
    destination = catalog.make_storage_location(
        location_type="gcs", is_backup_target=True, path="gs://my-bucket/sub"
    )
    backend = backend_for_destination(db_session, destination)
    assert isinstance(backend, GCSBackend)
    assert backend._bucket_name == "my-bucket"


def test_legacy_gcs_destination_falls_back_to_cloud_config_bucket(db_session, catalog, cloud_storage_config_state):
    set_cloud_config(db_session, bucket_name="fallback-bucket")
    destination = catalog.make_storage_location(location_type="gcs", is_backup_target=True, path="")
    backend = backend_for_destination(db_session, destination)
    assert isinstance(backend, GCSBackend)
    assert backend._bucket_name == "fallback-bucket"


def test_missing_gcs_config_raises_storage_config_error(db_session, catalog):
    destination = catalog.make_storage_location(location_type="gcs", is_backup_target=True, path="gs://my-bucket/sub")
    with pytest.raises(StorageConfigError, match="Google Cloud"):
        backend_for_destination(db_session, destination)


def test_s3_destination_builds_an_s3_backend(db_session, catalog):
    from app.models import S3StorageConfig

    config = S3StorageConfig(
        connected=True, access_key_id="AKIA-test", secret_access_key="secret", region="us-east-1"
    )
    db_session.add(config)
    db_session.commit()
    try:
        destination = catalog.make_storage_location(
            location_type="s3", is_backup_target=True, path="s3://my-bucket/sub"
        )
        backend = backend_for_destination(db_session, destination)
        assert isinstance(backend, S3Backend)
        assert backend._bucket_name == "my-bucket"
    finally:
        db_session.delete(config)
        db_session.commit()


def test_missing_s3_config_raises_storage_config_error(db_session, catalog):
    destination = catalog.make_storage_location(location_type="s3", is_backup_target=True, path="s3://my-bucket/sub")
    with pytest.raises(StorageConfigError, match="AWS S3"):
        backend_for_destination(db_session, destination)
