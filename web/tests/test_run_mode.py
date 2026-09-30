"""_validate_run_mode: allowlists the mode form field reaching BackupRun/
RestoreRun rows. Pure - no database."""
from app.main import _validate_run_mode


def test_replace_all_accepted():
    assert _validate_run_mode("replace_all") == "replace_all"


def test_replace_older_accepted():
    assert _validate_run_mode("replace_older") == "replace_older"


def test_missing_defaults_to_replace_older():
    assert _validate_run_mode(None) == "replace_older"


def test_invalid_value_falls_back_to_default():
    assert _validate_run_mode("drop-tables") == "replace_older"
    assert _validate_run_mode("") == "replace_older"
