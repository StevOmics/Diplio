from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://mediabridge:mediabridge@db:5432/mediabridge"
    movies_root: str = "/mnt/Movies"
    media_root: str = "/mnt"
    secret_key: str = "dev-only-insecure-secret-key"
    rabbitmq_default_user: str = "mediabridge"
    rabbitmq_default_pass: str = "mediabridge"
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = 5672
    terminator_api_key: str = ""

    class Config:
        env_file = ".env"

    @property
    def broker_url(self) -> str:
        return f"amqp://{self.rabbitmq_default_user}:{self.rabbitmq_default_pass}@{self.rabbitmq_host}:{self.rabbitmq_port}//"


settings = Settings()

# Extensions cataloged for each StorageLocation.media_type. "movies" is the
# original, still-primary use case; the others let a location be typed as
# music/photos/documents instead so the same scan/catalog/backup pipeline
# works for them too. A location's own media_type picks which set applies -
# see extensions_for_media_type and catalog.py's _scan_location.
MEDIA_TYPE_EXTENSIONS: dict[str, set[str]] = {
    "movies": {"mp4", "m4v", "mkv", "avi", "mov", "wmv"},
    "music": {"mp3", "flac", "m4a", "aac", "wav", "ogg", "wma"},
    "photos": {"jpg", "jpeg", "png", "gif", "heic", "heif", "tiff", "bmp", "raw", "cr2", "nef", "dng"},
    "documents": {"pdf", "doc", "docx", "txt", "md", "epub", "rtf", "odt", "xls", "xlsx", "ppt", "pptx"},
}

# A location can also be typed "files" - not a key in MEDIA_TYPE_EXTENSIONS
# above (it has no fixed extension set of its own; it catalogs anything,
# still tagging recognized extensions with their real type and only using
# "files" itself as the misc/catch-all for what's left over - see
# extensions_for_media_type and media_type_for_extension).
MEDIA_TYPES = [*MEDIA_TYPE_EXTENSIONS.keys(), "files"]

# Never cataloged as content in its own right, even in a "files" location:
# .nfo sidecars (parsed separately, see nfo.py) and in-progress transfer
# artifacts the worker itself writes mid-copy (see worker/app/tasks.py).
IGNORED_EXTENSIONS = {"nfo", "mbcopy", "tmp"}

# Backward-compatible alias for the original single-type constant.
VIDEO_EXTENSIONS = MEDIA_TYPE_EXTENSIONS["movies"]


def extensions_for_media_type(media_type: str) -> set[str] | None:
    """Which extensions get cataloged when scanning a StorageLocation of this
    type. None means no restriction - "files" catalogs anything (each file is
    then tagged individually by media_type_for_extension). An
    unrecognized/legacy type falls back to "movies"."""
    if media_type == "files":
        return None
    return MEDIA_TYPE_EXTENSIONS.get(media_type, MEDIA_TYPE_EXTENSIONS["movies"])


def media_type_for_extension(extension: str) -> str | None:
    """Which specific type owns this extension, if any - used to tag a file
    in a "files" location with its real type when recognized. None if no
    type claims it (the caller should fall back to "files" as the misc
    catch-all, not skip the file - see catalog.py:_scan_location)."""
    for media_type, extensions in MEDIA_TYPE_EXTENSIONS.items():
        if extension in extensions:
            return media_type
    return None
