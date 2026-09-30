from app.config import (
    IGNORED_EXTENSIONS,
    MEDIA_TYPE_EXTENSIONS,
    MEDIA_TYPES,
    extensions_for_media_type,
    media_type_for_extension,
)


def test_media_types_includes_files_plus_every_declared_type():
    assert set(MEDIA_TYPES) == set(MEDIA_TYPE_EXTENSIONS.keys()) | {"files"}


def test_extensions_for_known_type_returns_only_that_types_extensions():
    assert extensions_for_media_type("movies") == MEDIA_TYPE_EXTENSIONS["movies"]
    assert extensions_for_media_type("photos") == MEDIA_TYPE_EXTENSIONS["photos"]


def test_extensions_for_files_is_unrestricted():
    # "files" has no fixed extension set of its own - None means "catalog
    # anything" (see catalog.py:_scan_location).
    assert extensions_for_media_type("files") is None


def test_extensions_for_unrecognized_type_falls_back_to_movies():
    # Legacy StorageLocation rows created before this column existed default
    # to "movies" at the DB level, but an unrecognized value should behave
    # the same way rather than erroring.
    assert extensions_for_media_type("something-invalid") == MEDIA_TYPE_EXTENSIONS["movies"]


def test_media_type_for_extension_matches_correct_type():
    assert media_type_for_extension("mp4") == "movies"
    assert media_type_for_extension("jpg") == "photos"
    assert media_type_for_extension("mp3") == "music"
    assert media_type_for_extension("pdf") == "documents"


def test_media_type_for_extension_unrecognized_returns_none():
    # Caller (catalog.py) falls back to "files" as the misc catch-all rather
    # than skipping the file - see test_scan behavior in test_catalog_helpers.
    assert media_type_for_extension("xyz123") is None


def test_no_extension_claimed_by_more_than_one_type():
    seen = {}
    for media_type, extensions in MEDIA_TYPE_EXTENSIONS.items():
        for ext in extensions:
            assert ext not in seen, f"{ext!r} claimed by both {seen.get(ext)!r} and {media_type!r}"
            seen[ext] = media_type


def test_ignored_extensions_are_not_claimed_by_any_media_type():
    # .nfo/.mbcopy/.tmp must never be cataloged as content, even in a "files"
    # location - see catalog.py:_scan_location.
    all_extensions = set().union(*MEDIA_TYPE_EXTENSIONS.values())
    assert not (IGNORED_EXTENSIONS & all_extensions)
