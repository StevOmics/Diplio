"""Upload bandwidth limit settings (Settings > Cloud Storage > Throughput
Analysis): the effective-speed display/ETA math and the "Speed limit" form's
validation. Mirrors worker/app/gcs.py:effective_upload_mbps, which actually
enforces the cap - see worker/tests/test_upload_bandwidth.py for that side.
"""
from app.backup_settings import parse_upload_limit
from app.main import CLOUD_UPLOAD_THROTTLE_FRACTION, DEFAULT_UPLOAD_CAP_MBPS, _effective_upload_mbps


# --- _effective_upload_mbps: what's in effect, for the display/ETA ----------


def test_falls_back_to_default_cap_with_no_measurement_and_no_override():
    assert _effective_upload_mbps(None, None) == DEFAULT_UPLOAD_CAP_MBPS


def test_uses_half_of_measured_speed_when_no_override():
    assert _effective_upload_mbps(40.0, None) == 40.0 * CLOUD_UPLOAD_THROTTLE_FRACTION


def test_explicit_override_wins_even_when_lower_than_the_automatic_default():
    assert _effective_upload_mbps(40.0, 5.0) == 5.0


def test_explicit_override_wins_when_equal_to_the_measured_ceiling():
    # The route caps an override at the measured speed, so "at the ceiling"
    # (not above it) is the highest override that can ever reach here.
    assert _effective_upload_mbps(40.0, 40.0) == 40.0


def test_zero_or_falsy_override_is_treated_as_unset():
    assert _effective_upload_mbps(40.0, 0) == 40.0 * CLOUD_UPLOAD_THROTTLE_FRACTION
    assert _effective_upload_mbps(40.0, None) == 40.0 * CLOUD_UPLOAD_THROTTLE_FRACTION


def test_never_returns_none_or_zero():
    for measured in (None, 0, 12.5):
        for override in (None, 0):
            assert _effective_upload_mbps(measured, override) > 0


# --- parse_upload_limit: the "Speed limit" form's validation ---------------


def test_blank_clears_the_override():
    assert parse_upload_limit("", measured_upload_mbps=40.0) == (None, None)
    assert parse_upload_limit("   ", measured_upload_mbps=None) == (None, None)


def test_accepts_a_value_below_the_measured_speed():
    assert parse_upload_limit("5", measured_upload_mbps=40.0) == (5.0, None)


def test_accepts_a_value_at_the_measured_ceiling():
    assert parse_upload_limit("40", measured_upload_mbps=40.0) == (40.0, None)


def test_rejects_a_value_above_the_measured_ceiling():
    value, error = parse_upload_limit("50", measured_upload_mbps=40.0)
    assert value is None
    assert error is not None and "40.0" in error


def test_no_ceiling_when_nothing_has_been_measured_yet():
    assert parse_upload_limit("500", measured_upload_mbps=None) == (500.0, None)


def test_rejects_non_numeric_input():
    value, error = parse_upload_limit("fast please", measured_upload_mbps=None)
    assert value is None and error == "Upload+limit+must+be+a+number"


def test_rejects_zero_and_negative_values():
    for raw in ("0", "-3"):
        value, error = parse_upload_limit(raw, measured_upload_mbps=None)
        assert value is None and error == "Upload+limit+must+be+greater+than+0+Mbps"
