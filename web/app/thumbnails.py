import hashlib
from pathlib import Path

from PIL import Image, UnidentifiedImageError

# Ephemeral - regenerated on demand if missing (e.g. after a container
# restart), so this doesn't need to be a mounted/persistent volume.
THUMBNAIL_DIR = Path("/tmp/mediabridge-thumbnails")
THUMBNAIL_MAX_DIMENSION = 240
THUMBNAIL_JPEG_QUALITY = 80


def _thumbnail_path(media_file_id: int, fingerprint: str | None) -> Path:
    # fingerprint in the cache key so a file that changes content (a rescan
    # picks up a new fingerprint - see catalog.py) gets a fresh thumbnail
    # instead of serving the old one indefinitely from cache.
    key = f"{media_file_id}-{fingerprint or 'nofp'}"
    digest = hashlib.sha1(key.encode()).hexdigest()
    return THUMBNAIL_DIR / f"{digest}.jpg"


def get_or_create_thumbnail(source_path: Path, media_file_id: int, fingerprint: str | None) -> Path | None:
    """A small cached JPEG thumbnail of source_path, generated on first
    request and reused after. None if Pillow can't read the file as an image
    (corrupted, or a format Pillow doesn't support - e.g. HEIC without the
    optional pillow-heif plugin) - callers should fall back to a placeholder
    rather than erroring the whole gallery over one bad file."""
    dest = _thumbnail_path(media_file_id, fingerprint)
    if dest.is_file():
        return dest

    THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source_path) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMBNAIL_MAX_DIMENSION, THUMBNAIL_MAX_DIMENSION))
            tmp_path = dest.with_suffix(".tmp")
            img.save(tmp_path, "JPEG", quality=THUMBNAIL_JPEG_QUALITY)
            tmp_path.replace(dest)
    except (UnidentifiedImageError, OSError):
        return None
    return dest
