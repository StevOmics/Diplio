from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MediaFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uuid: str
    fingerprint: str | None
    path: str
    filename: str
    extension: str
    genre: str | None
    size_bytes: int
    scanned_at: datetime
    title: str | None
    overview: str | None
    year: int | None
    imdb_id: str | None
    tmdb_id: str | None
    rating: float | None
    runtime_minutes: int | None
    watched: bool
    is_missing: bool
    play_count: int
    last_played_at: datetime | None
    jellyfin_library: str | None
