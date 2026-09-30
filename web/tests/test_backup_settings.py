from app.backup_settings import normalize_prefix, parse_exclude_globs


def test_normalize_prefix_adds_trailing_slash():
    assert normalize_prefix("server01") == "server01/"


def test_normalize_prefix_strips_leading_slash():
    assert normalize_prefix("/server01/") == "server01/"


def test_normalize_prefix_idempotent():
    assert normalize_prefix("server01/") == "server01/"


def test_normalize_prefix_empty_is_none():
    assert normalize_prefix("") is None
    assert normalize_prefix("   ") is None
    assert normalize_prefix("/") is None
    assert normalize_prefix(None) is None


def test_normalize_prefix_keeps_nested():
    assert normalize_prefix("/a/b") == "a/b/"


def test_parse_exclude_globs_strips_and_drops_blanks():
    assert parse_exclude_globs("  **/.cache/**  \n\n*.part\n") == ["**/.cache/**", "*.part"]


def test_parse_exclude_globs_dedupes_preserving_order():
    assert parse_exclude_globs("a\nb\na") == ["a", "b"]


def test_parse_exclude_globs_empty():
    assert parse_exclude_globs("") == []
    assert parse_exclude_globs(None) == []
