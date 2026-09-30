from types import SimpleNamespace

from app.catalog import _library_for_path, _map_jellyfin_path, _parse_jellyfin_datetime


def _config(path_prefix_from="/media", path_prefix_to="/mnt"):
    return SimpleNamespace(path_prefix_from=path_prefix_from, path_prefix_to=path_prefix_to)


def test_map_jellyfin_path_swaps_prefix():
    config = _config()
    assert _map_jellyfin_path(config, "/media/Movies/Comedy/Foo.mp4") == "/mnt/Movies/Comedy/Foo.mp4"


def test_map_jellyfin_path_returns_none_when_prefix_does_not_match():
    config = _config()
    assert _map_jellyfin_path(config, "/other/Movies/Foo.mp4") is None


def test_map_jellyfin_path_custom_prefixes():
    config = _config(path_prefix_from="/data", path_prefix_to="/mnt/media")
    assert _map_jellyfin_path(config, "/data/TV/Show/ep1.mp4") == "/mnt/media/TV/Show/ep1.mp4"


def test_library_for_path_matches_correct_prefix():
    prefixes = [("/mnt/Movies", "Movies"), ("/mnt/TV", "Shows")]
    assert _library_for_path("/mnt/Movies/Comedy/Foo.mp4", prefixes) == "Movies"
    assert _library_for_path("/mnt/TV/Show/ep1.mp4", prefixes) == "Shows"


def test_library_for_path_no_match_returns_none():
    prefixes = [("/mnt/Movies", "Movies")]
    assert _library_for_path("/mnt/Family/Foo.mp4", prefixes) is None


def test_parse_jellyfin_datetime_with_z_suffix():
    dt = _parse_jellyfin_datetime("2026-03-01T03:22:21.1165861Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 3 and dt.day == 1
    assert dt.tzinfo is not None


def test_parse_jellyfin_datetime_truncates_long_fractional_seconds():
    # Jellyfin sends 7-digit (100ns tick) fractions; Python's fromisoformat
    # needs <=6 digits, so this must not raise.
    dt = _parse_jellyfin_datetime("2026-03-01T03:22:21.1165861Z")
    assert dt.microsecond == 116586


def test_parse_jellyfin_datetime_without_fractional_seconds():
    dt = _parse_jellyfin_datetime("2026-03-01T03:22:21Z")
    assert dt is not None
    assert dt.second == 21


def test_parse_jellyfin_datetime_none_input():
    assert _parse_jellyfin_datetime(None) is None


def test_parse_jellyfin_datetime_empty_string():
    assert _parse_jellyfin_datetime("") is None


def test_parse_jellyfin_datetime_garbage_returns_none():
    assert _parse_jellyfin_datetime("not a date") is None
