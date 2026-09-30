from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app import jellyfin
from app.config import IGNORED_EXTENSIONS, extensions_for_media_type, media_type_for_extension
from app.fingerprint import compute_fingerprint
from app.models import BackupRecord, JellyfinConfig, MediaFile, StorageLocation
from app.nfo import parse_nfo


def scan_library(db: Session) -> dict:
    totals = {"found": 0, "added": 0, "updated": 0, "missing": 0, "changed": 0, "merged": 0}
    changed_files: list[str] = []
    # The backup target holds copies of files already cataloged from their real
    # location, not a source library - scanning it would catalog those copies as
    # unrelated duplicate entries.
    locations = (
        db.query(StorageLocation).filter_by(location_type="local", is_backup_target=False, untracked=False).all()
    )
    for location in locations:
        result = _scan_location(db, location)
        changed_files.extend(result.pop("changed_files"))
        for key, value in result.items():
            totals[key] += value
    totals["changed"] = len(changed_files)
    totals["changed_files"] = changed_files
    return totals


def _scan_location(db: Session, location: StorageLocation) -> dict:
    root = Path(location.path)
    found = added = updated = 0
    changed_files: list[str] = []

    # An unreachable root (unmounted drive, renamed folder) must not flag the
    # whole library as missing, so bail out before touching any rows.
    if not root.is_dir():
        return {"found": 0, "added": 0, "updated": 0, "missing": 0, "changed_files": []}

    seen_paths: set[str] = set()

    allowed_extensions = extensions_for_media_type(location.media_type)
    is_catch_all = location.media_type == "files"

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        extension = path.suffix.lower().lstrip(".")
        if extension in IGNORED_EXTENSIONS:
            continue
        if allowed_extensions is not None and extension not in allowed_extensions:
            continue
        # A "files" location tags each file with its real type when the
        # extension is recognized, and falls back to "files" itself (the
        # misc catch-all) otherwise - nothing gets skipped. A single-type
        # location just stamps every file with that type directly.
        media_type = (media_type_for_extension(extension) or "files") if is_catch_all else location.media_type

        found += 1
        rel_parts = path.relative_to(root).parts
        genre = rel_parts[0] if len(rel_parts) > 1 else None
        stat = path.stat()
        size_bytes = stat.st_size
        mtime_ns = stat.st_mtime_ns
        path_str = str(path)
        seen_paths.add(path_str)
        # NFO sidecars are a movie-specific convention (see nfo.py) - only
        # worth looking for next to movie files, not music/photos/documents/files.
        nfo_fields = parse_nfo(path.with_suffix(".nfo")) if media_type == "movies" else None
        nfo_fields = nfo_fields or {}

        existing = db.query(MediaFile).filter_by(path=path_str).one_or_none()
        if existing:
            size_changed = existing.size_bytes != size_bytes
            # mtime_ns is None only for a row that predates this column - not
            # a real change, just nothing to compare against yet.
            mtime_changed = existing.mtime_ns is not None and existing.mtime_ns != mtime_ns
            content_changed = size_changed or mtime_changed
            existing.size_bytes = size_bytes
            existing.mtime_ns = mtime_ns
            existing.genre = genre
            existing.media_type = media_type
            existing.storage_location_id = location.id
            existing.is_missing = False
            for key, value in nfo_fields.items():
                setattr(existing, key, value)
            if content_changed or not existing.fingerprint:
                existing.fingerprint = compute_fingerprint(path, size_bytes)
            if content_changed:
                changed_files.append(path_str)
            updated += 1
        else:
            db.add(
                MediaFile(
                    path=path_str,
                    filename=path.name,
                    extension=extension,
                    genre=genre,
                    size_bytes=size_bytes,
                    mtime_ns=mtime_ns,
                    media_type=media_type,
                    storage_location_id=location.id,
                    fingerprint=compute_fingerprint(path, size_bytes),
                    **nfo_fields,
                )
            )
            added += 1

    # Rows not seen this pass are only flagged if the file is really gone (a
    # changed extension filter or exclude shouldn't mark a present file missing).
    missing = 0
    for row in db.query(MediaFile).filter_by(storage_location_id=location.id, is_missing=False):
        if row.path not in seen_paths and not Path(row.path).exists():
            row.is_missing = True
            missing += 1

    db.commit()
    merged = _merge_stale_duplicates(db, location)
    return {
        "found": found,
        "added": added,
        "updated": updated,
        "missing": missing,
        "changed_files": changed_files,
        "merged": merged,
    }


def _merge_stale_duplicates(db: Session, location: StorageLocation) -> int:
    """Self-heals MediaFile rows left over from a library path change that
    didn't rewrite them (a bug that existed until this was added - see
    docs/CHANGES.md; a library's path can still be edited outside the app,
    e.g. directly in the database, so this stays as an ongoing safety net,
    not a one-off migration). A row whose path no longer starts with this
    location's current root is stale; if a live row (path under the current
    root) with the same filename+fingerprint exists, they're the same
    physical file catalogued twice. Whichever row carries any BackupRecord
    history is kept (repointed to the live path) so a completed backup never
    gets orphaned from the file that's actually still there; the redundant
    row is removed. If both sides have backup history that's a genuine
    ambiguity, left untouched for manual review rather than guessed at."""
    root_prefix = location.path.rstrip("/") + "/"
    stale_rows = (
        db.query(MediaFile)
        .filter(MediaFile.storage_location_id == location.id)
        .filter(MediaFile.fingerprint.isnot(None))
        .filter(~MediaFile.path.like(root_prefix + "%"))
        .all()
    )
    if not stale_rows:
        return 0

    live_by_key: dict[tuple[str, str], list[MediaFile]] = {}
    for mf in (
        db.query(MediaFile)
        .filter(MediaFile.storage_location_id == location.id)
        .filter(MediaFile.fingerprint.isnot(None))
        .filter(MediaFile.path.like(root_prefix + "%"))
    ):
        live_by_key.setdefault((mf.filename, mf.fingerprint), []).append(mf)

    def has_backup(media_file_id: int) -> bool:
        return db.query(BackupRecord.id).filter_by(media_file_id=media_file_id).first() is not None

    # Deletes are staged and flushed before any path reassignment below - a
    # keeper row taking over a dropped row's path would otherwise collide
    # with it under the unique index, since a flush issues all pending
    # UPDATEs before DELETEs regardless of the order they were queued in.
    to_delete: list[MediaFile] = []
    reassignments: list[tuple[MediaFile, str, int, int | None, datetime]] = []
    for stale in stale_rows:
        candidates = live_by_key.get((stale.filename, stale.fingerprint), [])
        if len(candidates) != 1:
            continue  # none, or ambiguous (more than one match) - don't guess
        live = candidates[0]
        stale_has_backup = has_backup(stale.id)
        live_has_backup = has_backup(live.id)
        if stale_has_backup and live_has_backup:
            continue  # both sides have real history - needs a human, not a guess
        if stale_has_backup or not live_has_backup:
            reassignments.append((stale, live.path, live.size_bytes, live.mtime_ns, live.scanned_at))
            to_delete.append(live)
        else:
            to_delete.append(stale)

    merged = len(to_delete)
    if not merged:
        return 0

    for row in to_delete:
        db.delete(row)
    db.flush()

    for stale, path, size_bytes, mtime_ns, scanned_at in reassignments:
        stale.path = path
        stale.is_missing = False
        stale.size_bytes = size_bytes
        stale.mtime_ns = mtime_ns
        stale.scanned_at = scanned_at

    db.commit()
    return merged


def _parse_jellyfin_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if "." in value:
        head, rest = value.split(".", 1)
        frac_digits = 0
        while frac_digits < len(rest) and rest[frac_digits].isdigit():
            frac_digits += 1
        frac = rest[:frac_digits][:6].ljust(6, "0")
        value = f"{head}.{frac}{rest[frac_digits:]}"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _map_jellyfin_path(config: JellyfinConfig, jf_path: str) -> str | None:
    if not jf_path.startswith(config.path_prefix_from):
        return None
    return config.path_prefix_to + jf_path[len(config.path_prefix_from):]


def _library_for_path(mapped_path: str, library_prefixes: list[tuple[str, str]]) -> str | None:
    for prefix, name in library_prefixes:
        if mapped_path.startswith(prefix):
            return name
    return None


def sync_watch_data(db: Session, config: JellyfinConfig) -> dict:
    items = jellyfin.fetch_movie_watch_data(config.server_url, config.api_key, config.sync_user_id)
    matched = unmatched = 0

    # Map each library's Jellyfin-side location into MediaBridge's path space,
    # same prefix swap used for individual items, so we can tell them apart.
    library_prefixes: list[tuple[str, str]] = []
    try:
        for lib in jellyfin.list_libraries(config.server_url, config.api_key):
            for location in lib["locations"]:
                mapped = _map_jellyfin_path(config, location)
                if mapped:
                    library_prefixes.append((mapped, lib["name"]))
    except httpx.HTTPError:
        pass

    for item in items:
        jf_path = item.get("Path")
        mapped_path = _map_jellyfin_path(config, jf_path) if jf_path else None
        if not mapped_path:
            unmatched += 1
            continue

        media_file = db.query(MediaFile).filter_by(path=mapped_path).one_or_none()
        if not media_file:
            unmatched += 1
            continue

        user_data = item.get("UserData", {})
        media_file.jellyfin_item_id = item.get("Id")
        media_file.jellyfin_library = _library_for_path(mapped_path, library_prefixes)
        media_file.watched = bool(user_data.get("Played", False))
        media_file.play_count = user_data.get("PlayCount", 0)
        media_file.last_played_at = _parse_jellyfin_datetime(user_data.get("LastPlayedDate"))
        matched += 1

    db.commit()
    return {"items": len(items), "matched": matched, "unmatched": unmatched}
