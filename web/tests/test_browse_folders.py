import pytest
from fastapi import HTTPException

from app.config import settings as app_settings
from app.main import _list_subfolders, _resolve_within_media_root


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "media_root", str(tmp_path))
    return tmp_path


def test_list_subfolders_returns_dirs_sorted_case_insensitively(tmp_path):
    (tmp_path / "banana").mkdir()
    (tmp_path / "Apple").mkdir()
    (tmp_path / "cherry.txt").write_text("not a dir")

    folders = _list_subfolders(tmp_path)

    assert [f["name"] for f in folders] == ["Apple", "banana"]
    assert folders[0]["path"] == str(tmp_path / "Apple")


def test_list_subfolders_filters_dotfiles_and_ignored_names(tmp_path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "$RECYCLE.BIN").mkdir()
    (tmp_path / "Movies").mkdir()

    folders = _list_subfolders(tmp_path)

    assert [f["name"] for f in folders] == ["Movies"]


def test_list_subfolders_missing_directory_returns_empty(tmp_path):
    assert _list_subfolders(tmp_path / "does-not-exist") == []


def test_resolve_within_media_root_defaults_to_media_root(media_root):
    assert _resolve_within_media_root(None) == media_root.resolve()


def test_resolve_within_media_root_allows_subfolder(media_root):
    sub = media_root / "Movies"
    sub.mkdir()

    assert _resolve_within_media_root(str(sub)) == sub.resolve()


def test_resolve_within_media_root_rejects_dotdot_traversal(media_root):
    outside = media_root.parent / "outside-media-root"
    outside.mkdir(exist_ok=True)

    with pytest.raises(HTTPException) as exc_info:
        _resolve_within_media_root(str(media_root / ".." / "outside-media-root"))

    assert exc_info.value.status_code == 403


def test_resolve_within_media_root_rejects_symlink_escape(media_root):
    outside = media_root.parent / "outside-media-root-symlink-target"
    outside.mkdir(exist_ok=True)
    link = media_root / "escape"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(HTTPException) as exc_info:
        _resolve_within_media_root(str(link))

    assert exc_info.value.status_code == 403


def test_resolve_within_media_root_rejects_non_directory(media_root):
    file_path = media_root / "notadir.txt"
    file_path.write_text("hi")

    with pytest.raises(HTTPException) as exc_info:
        _resolve_within_media_root(str(file_path))

    assert exc_info.value.status_code == 400
