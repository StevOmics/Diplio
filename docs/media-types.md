# Media types

Movies were the original and are still the primary use case, but the catalog/scan/backup pipeline is generic across content types — it operates on `MediaFile` rows (path, size, fingerprint) and doesn't actually care what's inside them. What makes a type "supported" is really just two things: an extension list, and (optionally) type-specific metadata enrichment. Backup, restore, verify, encryption, splitting, and clumping all work identically regardless of `media_type` already, with no changes needed per type.

## How it works

Every `StorageLocation` has a `media_type`: `"movies"`, `"music"`, `"photos"`, `"documents"`, or `"files"`. This decides what a scan of that location catalogs:

- A **single-type** location (`"movies"`, `"music"`, `"photos"`, `"documents"`) only catalogs files whose extension belongs to that type (`app/config.py:MEDIA_TYPE_EXTENSIONS`). Everything else in the folder is ignored. This is the original behavior, generalized — a `"movies"` location today behaves exactly like the old hardcoded video-only scan.
- A **`"files"`** location is the misc/catch-all type: it catalogs *anything*. A recognized extension still gets tagged with its real type (a `.jpg` in a `"files"` location is cataloged as `media_type = "photos"`, not `"files"`); only extensions that don't belong to any declared type fall back to `"files"` itself. Nothing is skipped.
- A few extensions are never cataloged as content at all, even in a `"files"` location — `.nfo` sidecars and `.mbcopy`/`.tmp` in-progress transfer artifacts (`app/config.py:IGNORED_EXTENSIONS`).

`MediaFile.media_type` is stamped from whichever type classified it during scan, and is what the catalog page's type filter and the extension display in Settings both read from.

## Current extension map

| Type | Extensions |
|---|---|
| `movies` | mp4, m4v, mkv, avi, mov, wmv |
| `music` | mp3, flac, m4a, aac, wav, ogg, wma |
| `photos` | jpg, jpeg, png, gif, heic, heif, tiff, bmp, raw, cr2, nef, dng |
| `documents` | pdf, doc, docx, txt, md, epub, rtf, odt, xls, xlsx, ppt, pptx |
| `files` | (none — catch-all; not a key in the extension map) |

(Source of truth: `web/app/config.py:MEDIA_TYPE_EXTENSIONS`.)

## Adding a new type

1. Add an entry to `MEDIA_TYPE_EXTENSIONS` in `web/app/config.py`.
2. That's it for cataloging — the new type is automatically selectable when adding/editing a `StorageLocation` in Settings, automatically scanned correctly (both as a single-type location and as part of `"files"` catch-all classification), and automatically shows up in the catalog's type filter once at least one file of that type exists.
3. Backup/restore/verify need nothing further — they operate on `MediaFile.path`/`size_bytes`/`fingerprint` regardless of type.

## What's deliberately *not* generalized (yet)

- **Rich per-type metadata.** Movies get `.nfo`-derived fields (title, year, IMDb/TMDb IDs, rating) and Jellyfin watch-status sync. Music/photos/documents/files get none of that today — just path, size, fingerprint, and the generic `genre` (parent-folder) grouping. EXIF extraction for photos and ID3 tags for music are natural follow-ups but aren't built; see [`TODO.md`](TODO.md).
- **Sidecar/junk filtering in `"files"` locations.** A movie folder often has `poster.jpg`/`fanart.jpg` sitting next to the video file. In a `"files"` location, those get cataloged as ordinary photo entries — there's no "this is a sidecar image for that movie" detection. Using a single-type `"movies"` location (the default, and what existing installs already have) avoids this entirely, since non-video extensions are ignored outright there.
- **UI depth per type.** The catalog table, filters, and detail view are still fairly movie-shaped (rating column, watched checkmark, etc.) even though the underlying data now supports other types. Non-movie fields just render as blank/`-` for now.
