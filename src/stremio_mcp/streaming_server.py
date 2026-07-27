from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import desktop
from .net import HttpError, get_json

_INFO_HASH_RE = re.compile(r"[0-9a-f]{40}")


class StreamingServerError(RuntimeError):
    pass


def _info_hash(value: str) -> str:
    normalized = value.strip().lower()
    if not _INFO_HASH_RE.fullmatch(normalized):
        raise StreamingServerError("info_hash must be a 40-character hexadecimal BitTorrent hash")
    return normalized


async def _active_stats() -> dict[str, Any]:
    return await get_json(f"{desktop.STREAMING_SERVER_URL}/stats.json")


async def active_streams(info_hash: str = "") -> list[dict[str, Any]]:
    stats = await _active_stats()
    if info_hash:
        normalized = _info_hash(info_hash)
        if normalized not in stats:
            raise StreamingServerError(f"no active torrent engine for {normalized}")
        stats = {normalized: stats[normalized]}

    streams = []
    for engine_hash, engine in stats.items():
        files = engine.get("files") or []
        file_stats = await asyncio.gather(
            *(
                get_json(
                    f"{desktop.STREAMING_SERVER_URL}/{engine_hash}/{file_index}/stats.json"
                )
                for file_index in range(len(files))
            )
        )
        total_bytes = sum(file["length"] for file in files)
        downloaded_bytes = engine["downloaded"]
        streams.append(
            {
                "info_hash": engine_hash,
                "name": engine.get("name"),
                "peers": engine["peers"],
                "downloaded_bytes": downloaded_bytes,
                "total_bytes": total_bytes,
                "progress": downloaded_bytes / total_bytes if total_bytes else 0,
                "download_speed_bps": engine["downloadSpeed"],
                "upload_speed_bps": engine["uploadSpeed"],
                "files": [
                    {
                        "index": file_index,
                        "name": file["name"],
                        "length_bytes": file["length"],
                        "progress": file_stats[file_index]["streamProgress"],
                    }
                    for file_index, file in enumerate(files)
                ],
            }
        )
    return streams


def _cache_root(settings: dict[str, Any]) -> Path:
    values = settings["values"]
    configured_root = Path(values["cacheRoot"]).expanduser().resolve()
    cache_root = (configured_root / "stremio-cache").resolve()
    if cache_root.parent != configured_root or cache_root.name != "stremio-cache":
        raise StreamingServerError("streaming server returned an unsafe cache root")
    return cache_root


def _entry_details(path: Path, active_hashes: set[str]) -> dict[str, Any]:
    if path.is_symlink() or not path.is_dir():
        raise StreamingServerError(f"unexpected cache entry: {path.name}")
    info_hash = _info_hash(path.name)
    size_bytes = 0
    file_count = 0
    latest_modified_ns = path.stat().st_mtime_ns
    for directory, directory_names, file_names in os.walk(path, followlinks=False):
        current = Path(directory)
        for directory_name in directory_names:
            child = current / directory_name
            if child.is_symlink():
                raise StreamingServerError(f"unexpected cache symlink: {child}")
        for file_name in file_names:
            child = current / file_name
            stat = child.stat(follow_symlinks=False)
            if child.is_symlink():
                raise StreamingServerError(f"unexpected cache symlink: {child}")
            size_bytes += stat.st_size
            file_count += 1
            latest_modified_ns = max(latest_modified_ns, stat.st_mtime_ns)
    return {
        "info_hash": info_hash,
        "size_bytes": size_bytes,
        "file_count": file_count,
        "modified_at": latest_modified_ns / 1_000_000_000,
        "active": info_hash in active_hashes,
    }


async def cache_inventory() -> dict[str, Any]:
    settings, stats = await asyncio.gather(desktop.server_settings(), _active_stats())
    cache_root = _cache_root(settings)
    entries = (
        sorted(
            (_entry_details(path, set(stats)) for path in cache_root.iterdir()),
            key=lambda entry: entry["modified_at"],
            reverse=True,
        )
        if cache_root.exists()
        else []
    )
    return {
        "cache_root": str(cache_root),
        "configured_limit_bytes": settings["values"]["cacheSize"],
        "used_bytes": sum(entry["size_bytes"] for entry in entries),
        "entry_count": len(entries),
        "entries": entries,
    }


async def _stop_engines(info_hash: str) -> None:
    route = f"/{info_hash}/remove" if info_hash else "/removeAll"
    await get_json(f"{desktop.STREAMING_SERVER_URL}{route}")
    for _ in range(50):
        remaining = await _active_stats()
        if info_hash not in remaining if info_hash else not remaining:
            return
        await asyncio.sleep(0.1)
    target = info_hash or "all torrent engines"
    raise StreamingServerError(f"streaming server did not stop {target}")


async def purge_cache(confirm: bool, info_hash: str = "") -> dict[str, Any]:
    if not confirm:
        raise StreamingServerError("set confirm=true to purge cached torrent data")
    normalized = _info_hash(info_hash) if info_hash else ""
    inventory = await cache_inventory()
    entries = inventory["entries"]
    if normalized:
        entries = [entry for entry in entries if entry["info_hash"] == normalized]
        active = await _active_stats()
        if not entries and normalized not in active:
            raise StreamingServerError(f"no active engine or cached data for {normalized}")

    await _stop_engines(normalized)
    cache_root = Path(inventory["cache_root"])
    deleted_hashes = []
    deleted_bytes = 0
    for entry in entries:
        target = (cache_root / entry["info_hash"]).resolve()
        if target.parent != cache_root or target.name != entry["info_hash"]:
            raise StreamingServerError(f"unsafe cache purge target: {target}")
        shutil.rmtree(target)
        deleted_hashes.append(entry["info_hash"])
        deleted_bytes += entry["size_bytes"]
    return {
        "purged": True,
        "scope": normalized or "all",
        "deleted_hashes": deleted_hashes,
        "deleted_entries": len(deleted_hashes),
        "deleted_bytes": deleted_bytes,
    }


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_streaming_server_streams(info_hash: str = "") -> str:
        """List active torrent engines, transfer rates and per-file download progress.

        The Stremio desktop app must be running. Pass an info_hash to inspect one
        engine, or leave it empty to inspect every active torrent.
        """
        try:
            streams = await active_streams(info_hash)
        except (HttpError, StreamingServerError, KeyError, TypeError) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, "count": len(streams), "streams": streams})

    @mcp.tool()
    async def stremio_streaming_server_cache() -> str:
        """Show the desktop streaming server's cache limit, usage and torrent entries."""
        try:
            inventory = await cache_inventory()
        except (HttpError, StreamingServerError, KeyError, OSError, TypeError) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **inventory})

    @mcp.tool()
    async def stremio_streaming_server_purge_cache(
        confirm: bool = False, info_hash: str = ""
    ) -> str:
        """Delete cached torrent data after stopping the matching torrent engines.

        Set confirm=true. Pass one info_hash to purge that torrent, or leave it
        empty to purge the complete desktop torrent cache.
        """
        try:
            result = await purge_cache(confirm, info_hash)
        except (HttpError, StreamingServerError, KeyError, OSError, TypeError) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **result})
