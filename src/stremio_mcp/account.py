"""Stremio account and library tools over the official api.strem.io endpoints.

Reads and edits the signed-in user's library (add / remove / list) and can log
in with email and password to obtain an auth key. The auth key comes from the
``STREMIO_AUTH_KEY`` setting or from a successful ``stremio_login`` call made
during the process lifetime.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .config import settings

API_BASE = "https://api.strem.io/api/"
CINEMETA = "https://v3-cinemeta.strem.io"
COLLECTION = "libraryItem"

# Auth key obtained at runtime via stremio_login; overrides the env setting.
_session_auth_key: Optional[str] = None


class AccountError(RuntimeError):
    """Raised when an account API call fails or no auth key is available."""


def _current_key() -> str:
    key = _session_auth_key or settings.stremio_auth_key
    if not key:
        raise AccountError(
            "No Stremio auth key. Set STREMIO_AUTH_KEY or call stremio_login first."
        )
    return key


async def _api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(API_BASE + method, json=payload)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise AccountError(message or f"{method} failed")
    return data


async def _cinemeta_meta(imdb_id: str, content_type: str) -> dict[str, Any]:
    url = f"{CINEMETA}/meta/{content_type}/{imdb_id}.json"
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "stremio-mcp"}) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.json().get("meta", {}) or {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_state() -> dict[str, Any]:
    return {
        "lastWatched": "", "timeWatched": 0, "timeOffset": 0, "overallTimeWatched": 0,
        "timesWatched": 0, "flaggedWatched": 0, "duration": 0, "video_id": "",
        "watched": "", "noNotif": False, "season": 0, "episode": 0,
    }


def _library_item(imdb_id: str, content_type: str, meta: dict[str, Any]) -> dict[str, Any]:
    now = _now_iso()
    return {
        "_id": imdb_id, "removed": False, "temp": False, "_ctime": now, "_mtime": now,
        "state": _new_state(), "name": meta.get("name", ""), "type": content_type,
        "poster": meta.get("poster", ""), "posterShape": "poster",
        "background": meta.get("background", ""), "logo": meta.get("logo", ""),
        "year": str(meta.get("year", "") or ""),
    }


async def _get_items() -> list[dict[str, Any]]:
    data = await _api("datastoreGet", {"collection": COLLECTION, "all": True, "authKey": _current_key()})
    return data.get("result") or []


async def _put_items(changes: list[dict[str, Any]]) -> None:
    await _api("datastorePut", {"collection": COLLECTION, "changes": changes, "authKey": _current_key()})


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_login(email: str, password: str) -> str:
        """Log in to a Stremio account with email and password.

        On success stores the auth key for the rest of this server's lifetime and
        returns the account email. Accounts created via Facebook/Google can't use
        this; provide STREMIO_AUTH_KEY instead.
        """
        global _session_auth_key
        try:
            data = await _api("login", {"email": email, "password": password})
        except AccountError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        result = data.get("result") or {}
        key = result.get("authKey")
        if not key:
            return json.dumps({"ok": False, "error": "login returned no auth key"})
        _session_auth_key = key
        user = result.get("user") or {}
        return json.dumps({"ok": True, "email": user.get("email", email)})

    @mcp.tool()
    async def stremio_get_library(include_removed: bool = False) -> str:
        """List the account's saved library titles.

        Returns active (saved) items by default; pass include_removed=true to also
        list soft-deleted / watch-history entries.
        """
        try:
            items = await _get_items()
        except (AccountError, httpx.HTTPError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        selected = items if include_removed else [i for i in items if not i.get("removed")]
        library = [
            {"id": i.get("_id"), "name": i.get("name"), "type": i.get("type"),
             "year": i.get("year"), "poster": i.get("poster"),
             "removed": bool(i.get("removed"))}
            for i in selected
        ]
        return json.dumps({"ok": True, "count": len(library), "items": library})

    @mcp.tool()
    async def stremio_add_to_library(imdb_id: str, content_type: str = "movie") -> str:
        """Add a movie or series to the account library by IMDb id.

        Re-adding a previously removed title restores it and keeps its watch state.
        """
        if content_type not in ("movie", "series"):
            return json.dumps({"ok": False, "error": "content_type must be 'movie' or 'series'"})
        try:
            existing = {i.get("_id"): i for i in await _get_items()}
            if imdb_id in existing:
                item = existing[imdb_id]
                item["removed"] = False
                item["temp"] = False
                item["_mtime"] = _now_iso()
            else:
                meta = await _cinemeta_meta(imdb_id, content_type)
                item = _library_item(imdb_id, content_type, meta)
            await _put_items([item])
            return json.dumps({"ok": True, "added": imdb_id, "name": item.get("name")})
        except (AccountError, httpx.HTTPError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
    async def stremio_remove_from_library(imdb_id: str) -> str:
        """Remove a title from the account library by IMDb id (soft delete, keeps watch state)."""
        try:
            existing = {i.get("_id"): i for i in await _get_items()}
            item = existing.get(imdb_id)
            if item is None or item.get("removed"):
                return json.dumps({"ok": True, "removed": imdb_id, "note": "not in active library"})
            item["removed"] = True
            item["temp"] = True
            item["_mtime"] = _now_iso()
            await _put_items([item])
            return json.dumps({"ok": True, "removed": imdb_id, "name": item.get("name")})
        except (AccountError, httpx.HTTPError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
