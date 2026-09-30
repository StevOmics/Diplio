from pathlib import Path

from app.nfo import parse_nfo

VALID_NFO = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <plot>A tale of murder, lust, greed, revenge, and seafood.</plot>
  <title>A Fish Called Wanda</title>
  <rating>7.209</rating>
  <year>1988</year>
  <imdbid>tt0095159</imdbid>
  <tmdbid>623</tmdbid>
  <runtime>108</runtime>
  <genre>Comedy</genre>
  <genre>Crime</genre>
</movie>
"""


def test_parses_all_expected_fields(tmp_path: Path):
    nfo = tmp_path / "movie.nfo"
    nfo.write_text(VALID_NFO, encoding="utf-8")

    result = parse_nfo(nfo)

    assert result == {
        "title": "A Fish Called Wanda",
        "overview": "A tale of murder, lust, greed, revenge, and seafood.",
        "year": 1988,
        "imdb_id": "tt0095159",
        "tmdb_id": "623",
        "rating": 7.209,
        "runtime_minutes": 108,
    }


def test_missing_file_returns_none(tmp_path: Path):
    assert parse_nfo(tmp_path / "does_not_exist.nfo") is None


def test_malformed_xml_returns_none(tmp_path: Path):
    nfo = tmp_path / "broken.nfo"
    nfo.write_text("<movie><title>Unclosed", encoding="utf-8")
    assert parse_nfo(nfo) is None


def test_non_movie_root_returns_none(tmp_path: Path):
    nfo = tmp_path / "episode.nfo"
    nfo.write_text("<episodedetails><title>Not a movie</title></episodedetails>", encoding="utf-8")
    assert parse_nfo(nfo) is None


def test_missing_optional_fields_are_none(tmp_path: Path):
    nfo = tmp_path / "sparse.nfo"
    nfo.write_text("<movie><title>Bare Bones</title></movie>", encoding="utf-8")

    result = parse_nfo(nfo)

    assert result["title"] == "Bare Bones"
    assert result["year"] is None
    assert result["imdb_id"] is None
    assert result["rating"] is None


def test_non_numeric_rating_does_not_raise(tmp_path: Path):
    nfo = tmp_path / "bad_rating.nfo"
    nfo.write_text("<movie><title>X</title><rating>not-a-number</rating></movie>", encoding="utf-8")

    result = parse_nfo(nfo)

    assert result["rating"] is None
