"""Key history (web/app/key_versions.py): rotating the passphrase keeps the old
one. In-memory SQLite, no Postgres needed."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.encryption_check import derive_content_id, derive_master_key
from app.key_versions import backfill_content_ids, ensure_current_version, kdf_salt_is_valid_hex, key_history, set_passphrase
from app.models import (
    BackupArchive,
    BackupEncryptionConfig,
    BackupKeyVersion,
    BackupRecord,
    BackupRecordContentId,
    MediaFile,
    StorageLocation,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _config(db, password=None, salt=None):
    config = BackupEncryptionConfig(enabled=True, password=password, kdf_salt=salt)
    db.add(config)
    db.commit()
    return config


def test_first_passphrase_creates_current_version(db):
    config = _config(db)
    assert set_passphrase(db, config, "one") is True
    db.commit()
    rows = db.query(BackupKeyVersion).all()
    assert len(rows) == 1 and rows[0].retired_at is None
    assert rows[0].password == config.password == "one" and rows[0].kdf_salt == config.kdf_salt


def test_rotation_retires_previous_and_keeps_its_password(db):
    config = _config(db)
    set_passphrase(db, config, "one")
    first_salt = config.kdf_salt
    set_passphrase(db, config, "two")
    db.commit()

    old, new = db.query(BackupKeyVersion).order_by(BackupKeyVersion.id).all()
    assert (old.password, old.kdf_salt) == ("one", first_salt) and old.retired_at is not None
    assert new.password == config.password == "two" and new.retired_at is None
    assert new.kdf_salt != first_salt


def test_same_passphrase_is_a_no_op(db):
    config = _config(db)
    set_passphrase(db, config, "one")
    salt = config.kdf_salt
    assert set_passphrase(db, config, "one") is False
    db.commit()
    assert config.kdf_salt == salt and db.query(BackupKeyVersion).count() == 1


def test_preexisting_key_is_backfilled_before_first_rotation(db):
    config = _config(db, password="legacy", salt="ab" * 16)  # set before versions existed
    assert db.query(BackupKeyVersion).count() == 0
    set_passphrase(db, config, "new")
    db.commit()
    old = db.query(BackupKeyVersion).filter_by(password="legacy").one()
    assert old.retired_at is not None and old.kdf_salt == "ab" * 16


def test_history_is_newest_first_with_archive_counts(db):
    config = _config(db)
    set_passphrase(db, config, "one")
    db.commit()
    v1 = db.query(BackupKeyVersion).one()
    set_passphrase(db, config, "two")
    db.commit()
    location = StorageLocation(name="dest", path="gcs://b", location_type="gcs", media_type="movies")
    db.add(location)
    db.commit()
    db.add_all([BackupArchive(storage_location_id=location.id, path=f"p{i}", size_bytes=1, key_version_id=v1.id) for i in range(3)])
    db.commit()

    history = key_history(db, config)
    assert [h["number"] for h in history] == [2, 1]
    assert [h["current"] for h in history] == [True, False]
    assert [h["archives"] for h in history] == [0, 3]


def test_history_backfills_current_key_when_none_recorded(db):
    config = _config(db, password="legacy", salt="cd" * 16)
    history = key_history(db, config)
    assert len(history) == 1 and history[0]["current"] and history[0]["row"].password == "legacy"
    assert ensure_current_version(db, config).id == history[0]["row"].id  # not duplicated


def test_no_key_no_history(db):
    assert key_history(db, _config(db)) == []


# --- content_id backfill (docs/backup-plan/DEVIATIONS.md) ------------------


def _record_with_sha256(db, sha256: str) -> BackupRecord:
    location = StorageLocation(name="lib", path="/media", location_type="local", media_type="movies")
    dest = StorageLocation(name="dest", path="gcs://b", location_type="gcs", media_type="movies")
    db.add_all([location, dest])
    db.commit()
    media_file = MediaFile(
        storage_location_id=location.id, path=f"/media/{sha256}.mkv", filename=f"{sha256}.mkv", extension="mkv", size_bytes=1
    )
    db.add(media_file)
    db.commit()
    record = BackupRecord(
        media_file_id=media_file.id,
        destination_storage_location_id=dest.id,
        local_path=media_file.path,
        local_checksum="fp",
        sha256=sha256,
    )
    db.add(record)
    db.commit()
    return record


def test_backfill_content_ids_populates_every_record_with_a_sha256(db):
    record = _record_with_sha256(db, "a" * 64)
    master_key = derive_master_key("pw", b"0" * 16)
    backfill_content_ids(db, key_version_id=1, master_key=master_key)
    db.commit()

    rows = db.query(BackupRecordContentId).all()
    assert len(rows) == 1
    assert rows[0].backup_record_id == record.id
    assert rows[0].key_version_id == 1
    assert rows[0].content_id == derive_content_id(master_key, "a" * 64)


def test_backfill_content_ids_skips_records_already_covered_for_that_version(db):
    record = _record_with_sha256(db, "b" * 64)
    master_key = derive_master_key("pw", b"0" * 16)
    backfill_content_ids(db, key_version_id=1, master_key=master_key)
    db.commit()
    # Re-running for the same version must not duplicate the row.
    backfill_content_ids(db, key_version_id=1, master_key=master_key)
    db.commit()
    assert db.query(BackupRecordContentId).filter_by(backup_record_id=record.id, key_version_id=1).count() == 1


def test_rotation_backfills_content_ids_for_the_new_version(db):
    # The actual "track content across a master-key rotation" guarantee:
    # set_passphrase's rotation must trigger the backfill itself, not just
    # leave it as a manual step.
    config = _config(db)
    set_passphrase(db, config, "one")
    db.commit()
    record = _record_with_sha256(db, "c" * 64)

    set_passphrase(db, config, "two")
    db.commit()

    new_version = db.query(BackupKeyVersion).filter_by(retired_at=None).one()
    row = db.query(BackupRecordContentId).filter_by(backup_record_id=record.id, key_version_id=new_version.id).one()
    expected_key = derive_master_key("two", bytes.fromhex(config.kdf_salt))
    assert row.content_id == derive_content_id(expected_key, "c" * 64)


# --- portability: an explicit salt reproduces an old instance's exact key --


def test_explicit_salt_is_used_instead_of_a_random_one(db):
    config = _config(db)
    old_instance_salt = "ab" * 16
    assert set_passphrase(db, config, "reconnect-me", salt=old_instance_salt) is True
    assert config.kdf_salt == old_instance_salt
    db.commit()
    row = db.query(BackupKeyVersion).one()
    assert row.kdf_salt == old_instance_salt


def test_same_password_but_a_different_salt_is_not_a_no_op(db):
    # A prior save without a salt already minted a random one; supplying the
    # *real* salt afterward must still take effect, not be swallowed by the
    # "same password = no-op" short-circuit.
    config = _config(db)
    set_passphrase(db, config, "shared-password")
    random_salt = config.kdf_salt
    real_salt = "cd" * 16
    assert random_salt != real_salt
    assert set_passphrase(db, config, "shared-password", salt=real_salt) is True
    assert config.kdf_salt == real_salt
    db.commit()
    salts = {row.kdf_salt for row in db.query(BackupKeyVersion)}
    assert salts == {random_salt, real_salt}


def test_same_password_and_same_salt_is_still_a_no_op(db):
    config = _config(db)
    set_passphrase(db, config, "one")
    salt = config.kdf_salt
    assert set_passphrase(db, config, "one", salt=salt) is False
    db.commit()
    assert db.query(BackupKeyVersion).count() == 1


def test_no_salt_given_keeps_generating_a_random_one(db):
    config = _config(db)
    set_passphrase(db, config, "one")
    assert len(config.kdf_salt) == 32
    bytes.fromhex(config.kdf_salt)  # doesn't raise


# --- kdf_salt_is_valid_hex --------------------------------------------------


def test_valid_salt_shapes():
    assert kdf_salt_is_valid_hex("ab" * 16)
    assert kdf_salt_is_valid_hex("00" * 16)
    assert kdf_salt_is_valid_hex("ABCDEF01" * 4)  # case-insensitive at the hex level


def test_invalid_salt_shapes():
    assert not kdf_salt_is_valid_hex("")
    assert not kdf_salt_is_valid_hex("ab" * 15)  # too short
    assert not kdf_salt_is_valid_hex("ab" * 17)  # too long
    assert not kdf_salt_is_valid_hex("zz" * 16)  # not hex
    assert not kdf_salt_is_valid_hex("not a salt at all, just words")
