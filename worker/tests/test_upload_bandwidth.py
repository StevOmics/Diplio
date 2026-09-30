"""Upload bandwidth limiting: worker/app/gcs.py's effective-speed math, and
that a backup run actually passes the computed cap through to the storage
backend. See web/app/main.py's _effective_upload_mbps for the (mirrored)
web-side copy used for the Settings display and ETA.
"""
from __future__ import annotations

import pytest

from app.backup_run import _execute_backup_run
from app.gcs import (
    DEFAULT_UPLOAD_CAP_MBPS,
    UPLOAD_THROTTLE_FRACTION,
    effective_upload_mbps,
    upload_mbps_to_throttle_bytes_per_sec,
)
from tests.conftest import (
    set_cloud_config as _set_cloud_config,
    set_encryption_enabled as _set_encryption_enabled,
    set_transfer_config as _set_transfer_config,
    usable_destination as _usable_destination,
    write_file as _write_file,
)
from tests.storage_double import LocalBackend

pytestmark = pytest.mark.usefixtures(
    "encryption_config_state", "cloud_storage_config_state", "transfer_config_state"
)


# --- effective_upload_mbps: pure logic, no database -------------------------


def test_falls_back_to_default_cap_with_no_measurement_and_no_override():
    assert effective_upload_mbps(None, None) == DEFAULT_UPLOAD_CAP_MBPS


def test_uses_half_of_measured_speed_when_no_override():
    assert effective_upload_mbps(40.0, None) == 40.0 * UPLOAD_THROTTLE_FRACTION


def test_explicit_override_wins_even_when_lower_than_the_automatic_default():
    # The scenario from the request: cap it down to 5 Mbps even though the
    # automatic default (half of a measured 40 Mbps = 20) is much higher.
    assert effective_upload_mbps(40.0, 5.0) == 5.0


def test_explicit_override_wins_even_when_higher_than_the_automatic_default():
    assert effective_upload_mbps(10.0, 50.0) == 50.0


def test_explicit_override_applies_even_with_no_measurement_yet():
    assert effective_upload_mbps(None, 5.0) == 5.0


def test_zero_or_falsy_override_is_treated_as_unset():
    # Form fields round-trip through TransferConfig.max_upload_mbps as
    # None when cleared - 0/None must both mean "no override", not "0 Mbps".
    assert effective_upload_mbps(40.0, 0) == 40.0 * UPLOAD_THROTTLE_FRACTION
    assert effective_upload_mbps(40.0, None) == 40.0 * UPLOAD_THROTTLE_FRACTION


def test_never_returns_none_or_zero():
    for measured in (None, 0, 12.5):
        for override in (None, 0):
            assert effective_upload_mbps(measured, override) > 0


def test_bytes_per_sec_conversion_matches_the_mbps_value():
    mbps = upload_mbps_to_throttle_bytes_per_sec(None, 5.0)
    assert mbps == pytest.approx(5.0 * 1_000_000 / 8)


# --- wiring: a backup run passes the computed cap to the backend ------------


class _RecordingBackend:
    """Captures every upload() call's max_bytes_per_sec, then delegates."""

    def __init__(self, inner):
        self._inner = inner
        self.max_bytes_per_sec_calls: list[float | None] = []

    def upload(self, key, local_path, *, max_bytes_per_sec=None, progress_cb=None):
        self.max_bytes_per_sec_calls.append(max_bytes_per_sec)
        self._inner.upload(key, local_path, max_bytes_per_sec=max_bytes_per_sec, progress_cb=progress_cb)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _run_one_file(db_session, catalog, tmp_path, backend):
    source = catalog.make_storage_location(path=str(tmp_path))
    destination = _usable_destination(catalog)
    _set_encryption_enabled(db_session, False)
    _write_file(tmp_path, "a.txt", b"x" * 5000)
    catalog.make_media_file(
        path=str(tmp_path / "a.txt"), filename="a.txt", storage_location_id=source.id, size_bytes=5000
    )
    result = _execute_backup_run(source.id, destination.id, backend=backend, upload_sleep=lambda s: None)
    assert result["status"] == "ok"


def test_explicit_bandwidth_limit_reaches_the_upload_call(db_session, catalog, tmp_path):
    _set_cloud_config(db_session, upload_mbps=100.0)  # would default to 50 Mbps without an override
    _set_transfer_config(db_session, max_upload_mbps=5.0)

    backend = _RecordingBackend(LocalBackend(tmp_path / "bucket"))
    _run_one_file(db_session, catalog, tmp_path, backend)

    assert backend.max_bytes_per_sec_calls
    for value in backend.max_bytes_per_sec_calls:
        assert value == pytest.approx(5.0 * 1_000_000 / 8)


def test_no_override_uses_half_of_measured_speed(db_session, catalog, tmp_path):
    _set_cloud_config(db_session, upload_mbps=40.0)
    _set_transfer_config(db_session, max_upload_mbps=None)

    backend = _RecordingBackend(LocalBackend(tmp_path / "bucket"))
    _run_one_file(db_session, catalog, tmp_path, backend)

    for value in backend.max_bytes_per_sec_calls:
        assert value == pytest.approx(20.0 * 1_000_000 / 8)


def test_no_measurement_and_no_override_uses_the_conservative_default(db_session, catalog, tmp_path):
    _set_cloud_config(db_session, upload_mbps=None)
    _set_transfer_config(db_session, max_upload_mbps=None)

    backend = _RecordingBackend(LocalBackend(tmp_path / "bucket"))
    _run_one_file(db_session, catalog, tmp_path, backend)

    for value in backend.max_bytes_per_sec_calls:
        assert value == pytest.approx(DEFAULT_UPLOAD_CAP_MBPS * 1_000_000 / 8)
