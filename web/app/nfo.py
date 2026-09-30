import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional


def parse_nfo(nfo_path: Path) -> Optional[dict]:
    """Parse a Kodi/Jellyfin-style .nfo sidecar file into MediaFile fields."""
    try:
        root = ET.parse(nfo_path).getroot()
    except (ET.ParseError, OSError):
        return None

    if root.tag != "movie":
        return None

    def text(tag: str) -> Optional[str]:
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text else None

    def as_int(tag: str) -> Optional[int]:
        value = text(tag)
        try:
            return int(float(value)) if value else None
        except ValueError:
            return None

    def as_float(tag: str) -> Optional[float]:
        value = text(tag)
        try:
            return float(value) if value else None
        except ValueError:
            return None

    return {
        "title": text("title"),
        "overview": text("plot"),
        "year": as_int("year"),
        "imdb_id": text("imdbid"),
        "tmdb_id": text("tmdbid"),
        "rating": as_float("rating"),
        "runtime_minutes": as_int("runtime"),
    }
