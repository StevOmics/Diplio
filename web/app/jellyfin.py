import threading
import time
from typing import Callable

import httpx

_cache: dict[str, tuple[float, list[dict]]] = {}
_refresh_locks: dict[str, threading.Lock] = {}
CACHE_TTL_SECONDS = 300


def test_connection(server_url: str, api_key: str) -> dict:
    url = server_url.rstrip("/") + "/System/Info"
    try:
        response = httpx.get(url, headers={"X-Emby-Token": api_key}, timeout=5.0)
    except httpx.RequestError as exc:
        return {"ok": False, "message": f"Could not reach server: {exc}"}

    if response.status_code == 401:
        return {"ok": False, "message": "Rejected: invalid API key"}
    if response.status_code != 200:
        return {"ok": False, "message": f"Server responded with {response.status_code}"}

    data = response.json()
    server_name = data.get("ServerName", "Jellyfin")
    version = data.get("Version", "unknown")
    return {"ok": True, "message": f"Connected to {server_name} (version {version})"}


def list_users(server_url: str, api_key: str) -> list[dict]:
    url = server_url.rstrip("/") + "/Users"
    response = httpx.get(url, headers={"X-Emby-Token": api_key}, timeout=5.0)
    response.raise_for_status()
    return [{"id": u["Id"], "name": u["Name"]} for u in response.json()]


def _cached(cache_key: str, fetch: Callable[[], list[dict]]) -> list[dict]:
    """Serves the cached result for cache_key immediately (even if stale), and
    refreshes it on a background thread once it's older than CACHE_TTL_SECONDS.

    The Settings page calls list_users_cached/list_libraries_cached on every
    load. On this deployment, DNS resolution for the Jellyfin hostname alone
    takes several seconds (a property of the network, not of Jellyfin or
    httpx), so blocking page render directly on these calls made every
    Settings page view slow. This data rarely changes, so it's fine for it to
    be briefly stale while a refresh happens in the background - only the very
    first call ever for a given cache_key (nothing cached yet) still blocks,
    since there's nothing to serve.
    """
    now = time.monotonic()
    cached = _cache.get(cache_key)

    if cached is None:
        value = fetch()
        _cache[cache_key] = (now, value)
        return value

    age, value = now - cached[0], cached[1]
    if age >= CACHE_TTL_SECONDS:
        _refresh_in_background(cache_key, fetch)
    return value


def _refresh_in_background(cache_key: str, fetch: Callable[[], list[dict]]) -> None:
    lock = _refresh_locks.setdefault(cache_key, threading.Lock())
    if not lock.acquire(blocking=False):
        return  # a refresh for this cache_key is already in flight

    def _run() -> None:
        try:
            _cache[cache_key] = (time.monotonic(), fetch())
        except httpx.HTTPError:
            pass  # keep serving the stale cache; the next call will retry
        finally:
            lock.release()

    threading.Thread(target=_run, daemon=True).start()


def list_users_cached(server_url: str, api_key: str) -> list[dict]:
    return _cached(f"users|{server_url}|{api_key}", lambda: list_users(server_url, api_key))


def list_libraries(server_url: str, api_key: str) -> list[dict]:
    url = server_url.rstrip("/") + "/Library/VirtualFolders"
    response = httpx.get(url, headers={"X-Emby-Token": api_key}, timeout=5.0)
    response.raise_for_status()
    return [
        {"name": f["Name"], "locations": f.get("Locations", [])}
        for f in response.json()
    ]


def list_libraries_cached(server_url: str, api_key: str) -> list[dict]:
    return _cached(f"libraries|{server_url}|{api_key}", lambda: list_libraries(server_url, api_key))


def fetch_movie_watch_data(server_url: str, api_key: str, user_id: str) -> list[dict]:
    """Fetch every movie item + this user's watch data, handling pagination."""
    url = server_url.rstrip("/") + f"/Users/{user_id}/Items"
    headers = {"X-Emby-Token": api_key}
    items: list[dict] = []
    start_index = 0
    page_size = 200

    while True:
        response = httpx.get(
            url,
            headers=headers,
            timeout=15.0,
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "Path,UserData",
                "StartIndex": start_index,
                "Limit": page_size,
            },
        )
        response.raise_for_status()
        payload = response.json()
        page_items = payload.get("Items", [])
        items.extend(page_items)

        start_index += len(page_items)
        if len(page_items) < page_size or start_index >= payload.get("TotalRecordCount", 0):
            break

    return items
