"""A Stremio subtitle addon served from inside this MCP process.

Subtitles handed to the tools below are stored on disk and served over the LAN
by a small HTTP server that speaks the Stremio addon protocol, so they appear in
the subtitle picker on the Android TV or on a laptop. Installing the addon goes
through the account collection, which merges rather than replaces.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

import httpx

from . import addon_collection, api, subtitle_sync
from .config import settings
from .net import HttpError, get_bytes

ADDON_ID = "org.stremio.mcp-subtitles"
ADDON_BIND_HOST = "0.0.0.0"
ACCOUNT_REFRESH_SECONDS = 300

# video_id -> list of {id, lang, label, filename, size}
_subtitles: dict[str, list[dict[str, Any]]] = {}
_server: ThreadingHTTPServer | None = None
_server_port: int = 0
_index_mtime_ns: int = 0
_lock = threading.Lock()


def _local_address() -> str:
    """This machine's current LAN address, which is what the TV has to reach."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def base_url() -> str:
    return f"http://{_local_address()}:{_server_port}"


def manifest_url() -> str:
    return f"{base_url()}/manifest.json"


def video_id(imdb_id: str, season: int = 0, episode: int = 0) -> str:
    return f"{imdb_id}:{season}:{episode}" if season and episode else imdb_id


def _store_path(subtitle_id: str) -> Path:
    return settings.subtitle_dir / f"{subtitle_id}.srt"


def _manifest() -> dict[str, Any]:
    return {
        "id": ADDON_ID,
        "version": "1.2.0",
        "name": "MCP Subtitles",
        "description": (
            "Subtitles sideloaded through stremio-mcp, and Hebrew subtitles re-timed "
            "to the release that is playing."
        ),
        "catalogs": [],
        "resources": ["subtitles"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
    }


def lookup(requested_id: str) -> list[dict[str, Any]]:
    """Subtitles for a video id, falling back to ones filed under the bare title."""
    _reload_if_changed()
    with _lock:
        exact = list(_subtitles.get(requested_id, []))
        if exact:
            return exact
        return list(_subtitles.get(requested_id.split(":", 1)[0], []))


def parse_subtitles_path(path: str) -> tuple[str, str, dict[str, str]] | None:
    """Split /subtitles/{type}/{id}[/{extra}].json into type, video id and extras.

    Stremio appends an extra segment (videoHash, videoSize, filename) and series
    ids carry percent-encoded colons, so segments are split before decoding.
    """
    parts = [segment for segment in path.strip("/").split("/") if segment]
    if len(parts) not in (3, 4) or parts[0] != "subtitles" or not parts[-1].endswith(".json"):
        return None
    parts[-1] = parts[-1][: -len(".json")]
    extra = dict(parse_qsl(parts[3])) if len(parts) == 4 else {}
    return parts[1], unquote(parts[2]), extra


def _synced_entries(content_type: str, video_id: str, extra: dict[str, str]) -> list[dict[str, Any]]:
    try:
        synced = subtitle_sync.synced_subtitle(content_type, video_id, extra)
    except Exception:  # noqa: BLE001 - a failed sync must not hide the sideloaded subtitles
        print(f"[mcp-subtitles] sync failed for {video_id}:\n{traceback.format_exc()}", file=sys.stderr)
        return []
    if synced is None:
        return []
    return [
        {
            "id": f"synced-{synced.stem}",
            "url": f"{base_url()}/synced/{synced.stem}.srt",
            "lang": subtitle_sync.TARGET_LANGUAGE,
        }
    ]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/manifest.json":
            self._json(_manifest())
            return
        if path.startswith("/subtitles/"):
            parsed = parse_subtitles_path(path)
            if parsed is None:
                self._json({"subtitles": []})
                return
            content_type, requested, extra = parsed
            sideloaded = [
                {
                    "id": entry["id"],
                    "url": f"{base_url()}/subtitle/{entry['id']}.srt",
                    "lang": entry["lang"],
                }
                for entry in lookup(requested)
            ]
            self._json({"subtitles": _synced_entries(content_type, requested, extra) + sideloaded})
            return
        if path.startswith("/synced/") and path.endswith(".srt"):
            synced = subtitle_sync.synced_path(path.rsplit("/", 1)[-1][: -len(".srt")])
            if synced is None or not synced.is_file():
                self._not_found()
                return
            self._srt(synced.read_bytes(), synced.name)
            return
        if path.startswith("/subtitle/") and path.endswith(".srt"):
            subtitle_id = path.rsplit("/", 1)[-1][: -len(".srt")]
            stored = _store_path(subtitle_id)
            if not stored.is_file():
                self._not_found()
                return
            self._srt(stored.read_bytes(), stored.name)
            return
        self._not_found()

    def _headers(self, status: int, content_type: str, length: int, extra: tuple = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()

    def _json(self, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode()
        self._headers(200, "application/json", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _srt(self, body: bytes, filename: str) -> None:
        self._headers(
            200,
            "text/plain; charset=utf-8",
            len(body),
            (("Content-Disposition", f'attachment; filename="{filename}"'),),
        )
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_found(self) -> None:
        body = b'{"subtitles":[]}'
        self._headers(404, "application/json", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[mcp-subtitles] {self.address_string()} {format % args}", file=sys.stderr, flush=True)


def _serves_this_addon(port: int) -> bool:
    try:
        manifest = httpx.get(f"http://127.0.0.1:{port}/manifest.json", timeout=2.0).json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(manifest, dict) and manifest.get("id") == ADDON_ID


def start_server(fallback: bool = True) -> int:
    """Start the addon HTTP server once, returning the port it is served on.

    With fallback, a preferred port already serving this addon (the background
    subtitle service) is shared, and any other taken port moves us to a free one.
    """
    global _server, _server_port
    if _server_port:
        return _server_port
    try:
        _server = ThreadingHTTPServer((ADDON_BIND_HOST, settings.addon_port), _Handler)
    except OSError as error:
        if error.errno != errno.EADDRINUSE or not fallback:
            raise
        if _serves_this_addon(settings.addon_port):
            _server_port = settings.addon_port
            _load_from_disk()
            return _server_port
        _server = ThreadingHTTPServer((ADDON_BIND_HOST, 0), _Handler)
    _server_port = _server.server_address[1]
    threading.Thread(target=_server.serve_forever, daemon=True, name="mcp-subtitles").start()
    _load_from_disk()
    return _server_port


def stop_server() -> None:
    global _server, _server_port
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    _server_port = 0


def _index_path() -> Path:
    return settings.subtitle_dir / "index.json"


def _reload_if_changed() -> None:
    path = _index_path()
    if path.is_file() and path.stat().st_mtime_ns != _index_mtime_ns:
        _load_from_disk()


def _load_from_disk() -> None:
    global _index_mtime_ns
    path = _index_path()
    if not path.is_file():
        return
    try:
        _index_mtime_ns = path.stat().st_mtime_ns
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(stored, dict):
        return
    with _lock:
        _subtitles.clear()
        for key, entries in stored.items():
            kept = [entry for entry in entries if _store_path(entry["id"]).is_file()]
            if kept:
                _subtitles[key] = kept


def _save_to_disk() -> None:
    settings.subtitle_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        snapshot = json.dumps(_subtitles, ensure_ascii=False)
    _index_path().write_text(snapshot, encoding="utf-8")


async def install_to_account() -> dict[str, Any]:
    """Add this addon to the account collection without touching the rest of it."""
    current = await addon_collection.fetch()
    descriptor = {
        "transportUrl": manifest_url(),
        "transportName": "http",
        "manifest": _manifest(),
        "flags": {"official": False, "protected": False},
    }
    remaining = [
        entry for entry in current if addon_collection.descriptor_id(entry) != ADDON_ID
    ]
    updated = [descriptor] + remaining
    backup = await addon_collection._push(updated, current)
    return {"manifest_url": manifest_url(), "total_addons": len(updated), "backup": backup}


async def refresh_account_entry() -> bool:
    """Point the account at this server when its address or manifest changed."""
    if _local_address() == "127.0.0.1":
        print("[mcp-subtitles] offline, leaving the account entry alone", file=sys.stderr)
        return False
    installed = await addon_collection.fetch()
    ours = next((entry for entry in installed if addon_collection.descriptor_id(entry) == ADDON_ID), None)
    if (
        ours is not None
        and ours["transportUrl"] == manifest_url()
        and ours["manifest"].get("version") == _manifest()["version"]
    ):
        return False
    await install_to_account()
    return True


async def serve_forever() -> None:
    """Run the addon as a long-lived service that keeps the account pointed at it."""
    start_server(fallback=False)
    print(f"[mcp-subtitles] serving {manifest_url()}", file=sys.stderr, flush=True)
    while True:
        try:
            if await refresh_account_entry():
                print(f"[mcp-subtitles] account now uses {manifest_url()}", file=sys.stderr, flush=True)
        except Exception as error:  # noqa: BLE001 - keep serving through account API outages
            print(f"[mcp-subtitles] account refresh failed: {error}", file=sys.stderr, flush=True)
        await asyncio.sleep(ACCOUNT_REFRESH_SECONDS)


class SubtitleError(RuntimeError):
    pass


async def add_subtitle_url(
    imdb_id: str,
    subtitle_url: str,
    language: str = "heb",
    season: int = 0,
    episode: int = 0,
    label: str = "",
) -> dict[str, Any]:
    start_server()
    data = await get_bytes(subtitle_url, max_bytes=4_000_000)
    if not data.strip():
        raise SubtitleError("the downloaded subtitle is empty")

    key = video_id(imdb_id, season, episode)
    subtitle_id = hashlib.sha256(f"{key}|{language}".encode()).hexdigest()[:12]
    settings.subtitle_dir.mkdir(parents=True, exist_ok=True)
    _store_path(subtitle_id).write_bytes(data)

    entry = {
        "id": subtitle_id,
        "lang": language,
        "label": label or f"{language.upper()} (MCP)",
        "filename": f"{key.replace(':', '_')}_{language}.srt",
        "size": len(data),
    }
    with _lock:
        existing = _subtitles.setdefault(key, [])
        _subtitles[key] = [item for item in existing if item["id"] != subtitle_id] + [entry]
    _save_to_disk()

    synced: Any = "skipped: no auth key"
    if api.has_key():
        synced = await install_to_account()
    return {
        "video_id": key,
        "subtitle": entry,
        "manifest_url": manifest_url(),
        "account_sync": synced,
    }


def register(mcp) -> None:
    @mcp.tool(name="add_subtitle")
    async def add_subtitle(
        imdb_id: str,
        subtitle_url: str,
        language: str = "heb",
        season: int = 0,
        episode: int = 0,
        label: str = "",
    ) -> str:
        """Serve a subtitle file for a title through the MCP subtitle addon.

        subtitle_url is an http(s) link to an .srt file. Give season and episode
        to attach it to one episode; without them it covers the whole title.
        The subtitle then shows up in Stremio's subtitle picker on any device
        that can reach this machine on the LAN.
        """
        try:
            result = await add_subtitle_url(
                imdb_id, subtitle_url, language, season, episode, label
            )
        except (HttpError, OSError, RuntimeError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    @mcp.tool(name="list_subtitles")
    async def list_subtitles() -> str:
        """List every subtitle currently served by the MCP subtitle addon."""
        start_server()
        with _lock:
            items = {key: list(entries) for key, entries in _subtitles.items()}
        return json.dumps(
            {
                "ok": True,
                "count": sum(len(entries) for entries in items.values()),
                "items": items,
                "manifest_url": manifest_url(),
            },
            ensure_ascii=False,
        )

    @mcp.tool(name="remove_subtitle")
    async def remove_subtitle(
        imdb_id: str, subtitle_id: str = "", season: int = 0, episode: int = 0
    ) -> str:
        """Stop serving subtitles for a title. Omit subtitle_id to remove them all."""
        try:
            start_server()
            key = video_id(imdb_id, season, episode)
            with _lock:
                entries = _subtitles.get(key, [])
                doomed = [e for e in entries if not subtitle_id or e["id"] == subtitle_id]
                kept = [e for e in entries if e not in doomed]
                if kept:
                    _subtitles[key] = kept
                else:
                    _subtitles.pop(key, None)
            for entry in doomed:
                _store_path(entry["id"]).unlink(missing_ok=True)
            _save_to_disk()
        except OSError as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, "video_id": key, "removed": [e["id"] for e in doomed]})

    @mcp.tool(name="sync_addon")
    async def sync_addon() -> str:
        """Install the MCP subtitle addon onto the Stremio account.

        Run this if the addon is missing from the subtitle picker. Every other
        installed addon is left untouched.
        """
        start_server()
        try:
            result = await install_to_account()
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller, never swallowed
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)
