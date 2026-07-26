"""Client for the official Stremio account API at api.strem.io.

Holds the auth key for the process and exposes a single ``call`` entry point.
The key is taken from ``STREMIO_AUTH_KEY``, from a cached login on disk, or
from a ``stremio_login`` call made during this process's lifetime.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import settings
from .net import client

API_BASE = "https://api.strem.io/api/"

_session_auth_key: str | None = None
_cache_loaded = False


class ApiError(RuntimeError):
    """Raised when an account API call fails or no auth key is available."""


def _load_cached_key() -> str | None:
    path = settings.auth_cache_path
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("authKey") or None
    except (OSError, ValueError):
        return None


def _store_cached_key(key: str | None) -> None:
    path = settings.auth_cache_path
    try:
        if key is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"authKey": key}), encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass


def set_auth_key(key: str | None, persist: bool = True) -> None:
    global _session_auth_key, _cache_loaded
    _session_auth_key = key
    _cache_loaded = True
    if persist:
        _store_cached_key(key)


def current_key() -> str:
    global _session_auth_key, _cache_loaded
    if _session_auth_key:
        return _session_auth_key
    if settings.stremio_auth_key:
        return settings.stremio_auth_key
    if not _cache_loaded:
        _cache_loaded = True
        _session_auth_key = _load_cached_key()
    if _session_auth_key:
        return _session_auth_key
    raise ApiError("No Stremio auth key. Set STREMIO_AUTH_KEY or call stremio_login first.")


def has_key() -> bool:
    try:
        current_key()
    except ApiError:
        return False
    return True


async def call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST one api.strem.io method and return its decoded body.

    ``type`` is what stremio-core sends for every request; some methods reject
    the call without it.
    """
    body = {"type": method[0].upper() + method[1:], **payload}
    http = await client()
    try:
        response = await http.post(API_BASE + method, json=body, timeout=30.0)
    except httpx.HTTPError as error:
        raise ApiError(f"{method} request failed: {error}") from error
    if response.status_code != 200:
        raise ApiError(f"{method} returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as error:
        raise ApiError(f"{method} returned invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ApiError(f"{method} returned an unexpected shape")
    if data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise ApiError(message or f"{method} failed")
    return data


async def authed(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return await call(method, {"authKey": current_key(), **(payload or {})})
