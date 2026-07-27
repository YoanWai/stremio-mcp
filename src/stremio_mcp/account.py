"""Stremio account tools: sign in, and read or edit the synced library.

The library is one datastore collection of ``libraryItem`` records. Each record
carries a ``state`` block holding the resume position, so the same tools cover
the library list, continue-watching and marking things watched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import api
from .net import HttpError, get_json

CINEMETA = "https://v3-cinemeta.strem.io"
COLLECTION = "libraryItem"

# stremio-core treats an item as watched past this share of its duration.
WATCHED_THRESHOLD = 0.7


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_state() -> dict[str, Any]:
    return {
        "lastWatched": "",
        "timeWatched": 0,
        "timeOffset": 0,
        "overallTimeWatched": 0,
        "timesWatched": 0,
        "flaggedWatched": 0,
        "duration": 0,
        "video_id": "",
        "watched": "",
        "noNotif": False,
        "season": 0,
        "episode": 0,
    }


def _library_item(imdb_id: str, content_type: str, meta: dict[str, Any]) -> dict[str, Any]:
    now = _now_iso()
    return {
        "_id": imdb_id,
        "removed": False,
        "temp": False,
        "_ctime": now,
        "_mtime": now,
        "state": _new_state(),
        "name": meta.get("name", ""),
        "type": content_type,
        "poster": meta.get("poster", ""),
        "posterShape": "poster",
        "background": meta.get("background", ""),
        "logo": meta.get("logo", ""),
        "year": str(meta.get("year", "") or ""),
    }


def is_watched(item: dict[str, Any]) -> bool:
    state = item.get("state") or {}
    if state.get("flaggedWatched"):
        return True
    duration = state.get("duration") or 0
    return bool(duration) and state.get("timeWatched", 0) > duration * WATCHED_THRESHOLD


def _summarize(item: dict[str, Any]) -> dict[str, Any]:
    state = item.get("state") or {}
    duration = state.get("duration") or 0
    position = state.get("timeOffset") or 0
    return {
        "id": item.get("_id"),
        "name": item.get("name"),
        "type": item.get("type"),
        "year": item.get("year"),
        "poster": item.get("poster"),
        "removed": bool(item.get("removed")),
        "watched": is_watched(item),
        "video_id": state.get("video_id") or None,
        "season": state.get("season") or None,
        "episode": state.get("episode") or None,
        "position_ms": position,
        "duration_ms": duration,
        "progress": round(position / duration, 3) if duration else None,
        "last_watched": state.get("lastWatched") or None,
    }


async def _cinemeta_meta(imdb_id: str, content_type: str) -> dict[str, Any]:
    payload = await get_json(f"{CINEMETA}/meta/{content_type}/{imdb_id}.json")
    meta = payload.get("meta")
    return meta if isinstance(meta, dict) else {}


async def get_items() -> list[dict[str, Any]]:
    data = await api.authed("datastoreGet", {"collection": COLLECTION, "all": True})
    result = data.get("result")
    if not isinstance(result, list):
        raise api.ApiError("datastoreGet returned no library item list")
    return result


async def put_items(changes: list[dict[str, Any]]) -> None:
    await api.authed("datastorePut", {"collection": COLLECTION, "changes": changes})


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_login(email: str, password: str, remember: bool = True) -> str:
        """Sign in to a Stremio account with an email and password.

        The auth key is kept for this server's lifetime and, with remember, also
        cached in the user state directory so later runs stay signed in.
        Accounts created through Facebook or Google cannot use this; set
        STREMIO_AUTH_KEY for those.
        """
        try:
            data = await api.call("login", {"email": email, "password": password})
        except api.ApiError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        result = data.get("result") or {}
        key = result.get("authKey")
        if not key:
            return json.dumps({"ok": False, "error": "login returned no auth key"})
        api.set_auth_key(key, persist=remember)
        user = result.get("user") or {}
        return json.dumps({"ok": True, "email": user.get("email", email), "remembered": remember})

    @mcp.tool()
    async def stremio_logout() -> str:
        """Forget the stored Stremio auth key on this machine."""
        api.set_auth_key(None, persist=True)
        return json.dumps({"ok": True, "note": "STREMIO_AUTH_KEY from the environment still applies"})

    @mcp.tool()
    async def stremio_account_status() -> str:
        """Report whether a Stremio account is reachable and who is signed in."""
        if not api.has_key():
            return json.dumps({"ok": True, "authenticated": False})
        try:
            data = await api.authed("getUser")
        except api.ApiError as exc:
            return json.dumps({"ok": False, "authenticated": False, "error": str(exc)})
        user = data.get("result") or {}
        return json.dumps(
            {
                "ok": True,
                "authenticated": True,
                "email": user.get("email"),
                "premium_until": user.get("premium_expire"),
            }
        )

    @mcp.tool()
    async def stremio_get_library(
        include_removed: bool = False, content_type: str = "", watched: str = "any"
    ) -> str:
        """List the account's library titles.

        include_removed also lists soft-deleted entries, which is where watch
        history lives. content_type filters to 'movie' or 'series'. watched is
        'any', 'yes' or 'no'.
        """
        if watched not in ("any", "yes", "no"):
            return json.dumps({"ok": False, "error": "watched must be 'any', 'yes' or 'no'"})
        try:
            items = await get_items()
        except (api.ApiError, HttpError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        selected = items if include_removed else [i for i in items if not i.get("removed")]
        if content_type:
            selected = [i for i in selected if i.get("type") == content_type]
        if watched != "any":
            want = watched == "yes"
            selected = [i for i in selected if is_watched(i) == want]
        return json.dumps(
            {"ok": True, "count": len(selected), "items": [_summarize(i) for i in selected]},
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_continue_watching(limit: int = 20) -> str:
        """List part-watched titles with their resume positions, most recent first.

        This is the row Stremio shows on its home board. position_ms is where
        playback stopped, so it can be handed straight to a player.
        """
        try:
            items = await get_items()
        except (api.ApiError, HttpError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        started = [
            item
            for item in items
            if (item.get("state") or {}).get("timeOffset") and not is_watched(item)
        ]
        started.sort(key=lambda item: (item.get("state") or {}).get("lastWatched") or "", reverse=True)
        results = [_summarize(item) for item in started[: max(1, limit)]]
        return json.dumps({"ok": True, "count": len(results), "items": results}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_add_to_library(imdb_id: str, content_type: str = "movie") -> str:
        """Add a movie or series to the account library by IMDb id.

        Re-adding a title that was removed restores it with its watch state.
        """
        if content_type not in ("movie", "series"):
            return json.dumps({"ok": False, "error": "content_type must be 'movie' or 'series'"})
        try:
            existing = {item.get("_id"): item for item in await get_items()}
            item = existing.get(imdb_id)
            if item is not None:
                item["removed"] = False
                item["temp"] = False
                item["_mtime"] = _now_iso()
            else:
                item = _library_item(imdb_id, content_type, await _cinemeta_meta(imdb_id, content_type))
            await put_items([item])
        except (api.ApiError, HttpError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, "added": imdb_id, "name": item.get("name")}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_remove_from_library(imdb_id: str) -> str:
        """Remove a title from the account library. Watch state is kept."""
        try:
            existing = {item.get("_id"): item for item in await get_items()}
            item = existing.get(imdb_id)
            if item is None or item.get("removed"):
                return json.dumps({"ok": True, "removed": imdb_id, "note": "not in the active library"})
            item["removed"] = True
            item["temp"] = True
            item["_mtime"] = _now_iso()
            await put_items([item])
        except (api.ApiError, HttpError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, "removed": imdb_id, "name": item.get("name")}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_mark_watched(
        imdb_id: str, watched: bool = True, content_type: str = "movie"
    ) -> str:
        """Mark a library title watched or unwatched across every signed-in device.

        Marking it watched also clears the resume position so it leaves the
        continue-watching row.
        """
        if content_type not in ("movie", "series"):
            return json.dumps({"ok": False, "error": "content_type must be 'movie' or 'series'"})
        try:
            existing = {item.get("_id"): item for item in await get_items()}
            item = existing.get(imdb_id)
            if item is None:
                item = _library_item(imdb_id, content_type, await _cinemeta_meta(imdb_id, content_type))
            state = item.setdefault("state", _new_state())
            state["flaggedWatched"] = 1 if watched else 0
            if watched:
                state["timesWatched"] = max(1, state.get("timesWatched") or 0)
                state["lastWatched"] = _now_iso()
                state["timeOffset"] = 0
            else:
                state["timesWatched"] = 0
                state["timeWatched"] = 0
            item["_mtime"] = _now_iso()
            await put_items([item])
        except (api.ApiError, HttpError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {"ok": True, "id": imdb_id, "name": item.get("name"), "watched": watched},
            ensure_ascii=False,
        )
