"""Shared fixtures for the small number of database-backed tests in this
suite (test_backup_run.py, test_restore_v2.py). Everything else in
worker/tests is dependency-free and never imports this file's DB-touching
fixtures.

Gated on MEDIABRIDGE_TEST_DB=1 so `pytest -x -q` with nothing running still
passes (see docs/backup-plan/HANDOFF.md). Tests run against the same
Postgres the dev stack uses - there is no separate disposable test database -
so every fixture here is careful to touch only rows it created itself, and to
restore any singleton config row (BackupEncryptionConfig, CloudStorageConfig,
TransferConfig) to its prior state rather than leaving test values behind.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text


def _db_tests_enabled() -> bool:
    return os.environ.get("MEDIABRIDGE_TEST_DB") == "1"


@pytest.fixture
def db_session():
    if not _db_tests_enabled():
        pytest.skip("set MEDIABRIDGE_TEST_DB=1 to run database-backed tests")

    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _Catalog:
    """Creates StorageLocation/MediaFile rows scoped to one test and deletes
    exactly those rows afterward, in the order their foreign keys require
    (MediaFile before StorageLocation - MediaFile.storage_location_id has no
    ON DELETE CASCADE, unlike CopyJob/BackupArchive/BackupRecord)."""

    def __init__(self, db_session):
        self._db = db_session
        self._media_file_ids: list[int] = []
        self._storage_location_ids: list[int] = []

    def make_storage_location(self, **overrides):
        from app.models import StorageLocation

        tag = uuid.uuid4().hex[:12]
        defaults = dict(
            name=f"test-{tag}",
            location_type="local",
            media_type="files",
            path=f"/tmp/mb-test-{tag}",
            is_backup_target=False,
            exclude_globs=None,
        )
        defaults.update(overrides)
        location = StorageLocation(**defaults)
        self._db.add(location)
        self._db.commit()
        self._storage_location_ids.append(location.id)
        return location

    def make_media_file(self, **overrides):
        # Raw SQL, not the ORM: worker/app/models.py's MediaFile mirrors only
        # a subset of web/app/models.py's columns (see web/tests/model_sync.py).
        # extension/media_type/watched/play_count are now declared on it too
        # (worker/app/sync_run.py inserts fresh rows and needs them), but
        # plenty of other NOT NULL web-side columns still aren't mirrored, so
        # an ORM insert of a bare MediaFile(...) would still violate them.
        # Read back through the ORM afterward, since that's what
        # backup_run.py actually queries with.
        from sqlalchemy import text

        from app.models import MediaFile

        tag = uuid.uuid4().hex[:12]
        defaults = dict(
            uuid=tag,
            path=f"/tmp/mb-test-{tag}.bin",
            filename=f"{tag}.bin",
            extension="bin",
            media_type="files",
            size_bytes=0,
            fingerprint="deadbeef",
            storage_location_id=None,
            watched=False,
            play_count=0,
        )
        defaults.update(overrides)

        media_file_id = self._db.execute(
            text(
                "INSERT INTO media_files "
                "(uuid, path, filename, extension, media_type, size_bytes, fingerprint, "
                "storage_location_id, watched, play_count) "
                "VALUES (:uuid, :path, :filename, :extension, :media_type, :size_bytes, :fingerprint, "
                ":storage_location_id, :watched, :play_count) RETURNING id"
            ),
            defaults,
        ).scalar_one()
        self._db.commit()
        self._media_file_ids.append(media_file_id)
        return self._db.get(MediaFile, media_file_id)

    def cleanup(self):
        from app.models import MediaFile, StorageLocation

        self._db.rollback()
        if self._media_file_ids:
            self._db.query(MediaFile).filter(MediaFile.id.in_(self._media_file_ids)).delete(
                synchronize_session=False
            )
            self._db.commit()
        if self._storage_location_ids:
            self._db.query(StorageLocation).filter(
                StorageLocation.id.in_(self._storage_location_ids)
            ).delete(synchronize_session=False)
            self._db.commit()


@pytest.fixture
def catalog(db_session):
    c = _Catalog(db_session)
    yield c
    c.cleanup()


def _snapshot(db_session, model):
    row = db_session.query(model).first()
    if row is None:
        return None
    return {column.name: getattr(row, column.name) for column in model.__table__.columns}


def _restore(db_session, model, snapshot):
    db_session.rollback()
    rows = db_session.query(model).all()
    if snapshot is None:
        for row in rows:
            db_session.delete(row)
        db_session.commit()
        return

    row = rows[0] if rows else model()
    for extra in rows[1:]:
        db_session.delete(extra)
    if not rows:
        db_session.add(row)
    for key, value in snapshot.items():
        if key == "id":
            continue
        setattr(row, key, value)
    db_session.commit()


@pytest.fixture
def encryption_config_state(db_session):
    from app.models import BackupEncryptionConfig

    snapshot = _snapshot(db_session, BackupEncryptionConfig)
    yield
    _restore(db_session, BackupEncryptionConfig, snapshot)


@pytest.fixture
def cloud_storage_config_state(db_session):
    from app.models import CloudStorageConfig

    snapshot = _snapshot(db_session, CloudStorageConfig)
    yield
    _restore(db_session, CloudStorageConfig, snapshot)


@pytest.fixture
def transfer_config_state(db_session):
    from app.models import TransferConfig

    snapshot = _snapshot(db_session, TransferConfig)
    yield
    _restore(db_session, TransferConfig, snapshot)


# --- setup helpers shared by test_backup_run.py and test_restore_v2.py -----
# Plain functions, not fixtures, so a test can call them more than once with
# different arguments (e.g. flipping BackupEncryptionConfig.enabled back and
# forth) - only the initial/prior state needs a fixture (the *_config_state
# ones above), not each individual mutation.


def set_cloud_config(db_session, **overrides):
    from app.models import CloudStorageConfig

    config = db_session.query(CloudStorageConfig).first()
    if not config:
        # Raw SQL for the initial insert: worker/app/models.py's
        # CloudStorageConfig mirrors only a subset of web/app/models.py's
        # columns (deliberately - see web/tests/model_sync.py), and the real
        # table has provider/connected/is_backup_target NOT NULL with no
        # server-side default, so an ORM insert through the worker's mapped
        # class would violate those constraints.
        db_session.execute(
            text(
                "INSERT INTO cloud_storage_config (provider, connected, is_backup_target) "
                "VALUES ('gcs', false, false)"
            )
        )
        db_session.commit()
        config = db_session.query(CloudStorageConfig).first()
    defaults = dict(
        service_account_json='{"type": "service_account"}',
        bucket_name="test-bucket",
        project_id="test-project",
        prefix=None,
    )
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(config, key, value)
    db_session.commit()
    return config


def set_encryption_enabled(db_session, enabled: bool):
    from app.models import BackupEncryptionConfig

    config = db_session.query(BackupEncryptionConfig).first()
    if not config:
        config = BackupEncryptionConfig()
        db_session.add(config)
    config.enabled = enabled
    db_session.commit()


def set_encryption_key(db_session, password: str = "test-global-key"):
    """Enables backup encryption with a real password + salt, so the v2 run
    actually encrypts (set_encryption_enabled alone leaves it unusable)."""
    from app.models import BackupEncryptionConfig

    config = db_session.query(BackupEncryptionConfig).first()
    if not config:
        config = BackupEncryptionConfig()
        db_session.add(config)
    config.enabled = True
    config.password = password
    config.kdf_salt = "00112233445566778899aabbccddeeff"
    db_session.commit()


def set_transfer_config(db_session, **overrides):
    from app.models import TransferConfig

    config = db_session.query(TransferConfig).first()
    if not config:
        config = TransferConfig()
        db_session.add(config)
    for key, value in overrides.items():
        setattr(config, key, value)
    db_session.commit()


def write_file(tmp_path, rel_path: str, data: bytes) -> Path:
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def usable_destination(catalog):
    return catalog.make_storage_location(location_type="gcs", is_backup_target=True)
