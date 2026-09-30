import pytest

from app.archive_paths import format_gcs_path, is_gcs_path, normalize_prefix, parse_gcs_path


@pytest.mark.parametrize(
    "path,expected",
    [
        ("gs://my-bucket", ("my-bucket", "")),
        ("gs://my-bucket/", ("my-bucket", "")),
        ("gs://my-bucket/folder1", ("my-bucket", "folder1/")),
        ("gs://my-bucket/folder1/sub//deep/", ("my-bucket", "folder1/sub/deep/")),
        ("GS://my-bucket/a", ("my-bucket", "a/")),
        ("gcs://legacy_bucket", ("legacy_bucket", "")),  # legacy scheme still parses
    ],
)
def test_parse(path, expected):
    assert parse_gcs_path(path) == expected


@pytest.mark.parametrize("bad", ["", "my-bucket", "s3://b/x", "gs://", "gs://A_Bucket", "gs://a", "gs://-bad", "gs://bad-/x"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_gcs_path(bad)


def test_prefix_rejects_parent_traversal():
    with pytest.raises(ValueError):
        normalize_prefix("folder", "../other")


def test_normalize_prefix_joins_and_cleans():
    assert normalize_prefix("a/b", "/c/", None, "") == "a/b/c/"
    assert normalize_prefix() == ""
    assert normalize_prefix("a\\b") == "a/b/"


def test_format_and_is_gcs_path():
    assert format_gcs_path("b", "x/y/") == "gs://b/x/y"
    assert format_gcs_path("b") == "gs://b"
    assert is_gcs_path("gs://b") and is_gcs_path("gcs://b") and not is_gcs_path("/mnt/x") and not is_gcs_path(None)
