from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from . import addon_collection, api, desktop, tv
from .net import HttpError, client, get_json

_LEGACY_MANIFEST_PARAM = (
    "eyJwYXJhbXMiOltdLCJtZXRob2QiOiJtZXRhIiwiaWQiOjEsImpzb25ycGMiOiIyLjAifQ=="
)
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_INFO_HASH_RE = re.compile(r"[0-9a-f]{40}")
_CAST_TYPES = {"chromecast", "tv"}


class OperationsError(RuntimeError):
    pass


def _legacy_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    methods = raw["methods"]
    types = raw["types"]
    resources = []
    if "meta.get" in methods:
        resources.append("meta")
    if "stream.find" in methods:
        resources.append("stream")
    if "subtitles.find" in methods:
        resources.append("subtitles")

    catalogs = []
    if "meta.find" in methods:
        sorts = raw.get("sorts")
        if sorts:
            for sort in sorts:
                for content_type in sort.get("types") or types:
                    catalogs.append(
                        {
                            "type": content_type,
                            "id": sort["prop"],
                            "name": sort.get("name"),
                            "extra": [],
                        }
                    )
        else:
            catalogs = [
                {"type": content_type, "id": "top", "name": None, "extra": []}
                for content_type in types
            ]

    id_property = raw.get("idProperty")
    if isinstance(id_property, str):
        id_properties = [id_property]
    else:
        id_properties = id_property
    prefixes = (
        [
            "tt" if value == "imdb_id" else "UC" if value == "yt_id" else f"{value}:"
            for value in id_properties
        ]
        if id_properties
        else None
    )
    manifest = {
        "id": raw["id"],
        "name": raw["name"],
        "version": raw["version"],
        "resources": resources,
        "types": types,
        "catalogs": catalogs,
        "behaviorHints": {},
    }
    for source, target in (
        ("description", "description"),
        ("logo", "logo"),
        ("background", "background"),
        ("contactEmail", "contactEmail"),
    ):
        if raw.get(source) is not None:
            manifest[target] = raw[source]
    if prefixes is not None:
        manifest["idPrefixes"] = prefixes
    return manifest


async def live_manifest(entry: dict[str, Any]) -> dict[str, Any]:
    transport_url = entry["transportUrl"].rstrip("/")
    if transport_url.endswith("/stremio/v1"):
        payload = await get_json(
            f"{transport_url}/q.json?b={_LEGACY_MANIFEST_PARAM}"
        )
        result = payload.get("result")
        raw = result.get("manifest") if isinstance(result, dict) else None
        if not isinstance(raw, dict):
            raise OperationsError(f"{transport_url} returned no legacy manifest")
        return _legacy_manifest(raw)
    return await get_json(transport_url)


async def addon_health() -> list[dict[str, Any]]:
    installed = await addon_collection.fetch()

    async def check(entry):
        started = time.perf_counter()
        try:
            manifest = await live_manifest(entry)
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            installed_id = addon_collection.descriptor_id(entry)
            remote_id = manifest.get("id")
            if remote_id != installed_id:
                return {
                    "id": installed_id,
                    "name": entry["manifest"].get("name"),
                    "healthy": False,
                    "latency_ms": latency_ms,
                    "error": f"manifest id mismatch: {remote_id!r}",
                }
            return {
                "id": installed_id,
                "name": manifest.get("name"),
                "healthy": True,
                "latency_ms": latency_ms,
                "version": manifest.get("version"),
            }
        except (HttpError, OperationsError, KeyError, TypeError) as error:
            return {
                "id": addon_collection.descriptor_id(entry),
                "name": entry["manifest"].get("name"),
                "healthy": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": str(error),
            }

    return await asyncio.gather(*(check(entry) for entry in installed))


def _semver_key(version: str) -> tuple[Any, ...]:
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        raise OperationsError(f"invalid semantic version: {version!r}")
    prerelease = match[4]
    identifiers = (
        tuple(
            (0, int(identifier)) if identifier.isdigit() else (1, identifier)
            for identifier in prerelease.split(".")
        )
        if prerelease
        else ()
    )
    return (
        int(match[1]),
        int(match[2]),
        int(match[3]),
        0 if prerelease else 1,
        identifiers,
    )


async def addon_upgrades(apply: bool = False) -> dict[str, Any]:
    installed = await addon_collection.fetch()

    async def inspect(entry):
        try:
            remote = await live_manifest(entry)
            installed_version = entry["manifest"]["version"]
            remote_version = remote["version"]
            installed_key = _semver_key(installed_version)
            remote_key = _semver_key(remote_version)
            status = (
                "upgrade_available"
                if remote_key > installed_key
                else "remote_older"
                if remote_key < installed_key
                else "current"
            )
            return entry, remote, {
                "id": addon_collection.descriptor_id(entry),
                "name": entry["manifest"].get("name"),
                "installed_version": installed_version,
                "remote_version": remote_version,
                "status": status,
            }
        except (HttpError, OperationsError, KeyError, TypeError) as error:
            return entry, None, {
                "id": addon_collection.descriptor_id(entry),
                "name": entry["manifest"].get("name"),
                "status": "check_failed",
                "error": str(error),
            }

    outcomes = await asyncio.gather(*(inspect(entry) for entry in installed))
    checks = [check for _, _, check in outcomes]
    upgrades = {
        addon_collection.descriptor_id(entry): remote
        for entry, remote, check in outcomes
        if check["status"] == "upgrade_available"
    }
    backup = None
    if apply and upgrades:
        updated = []
        for entry in installed:
            addon_id = addon_collection.descriptor_id(entry)
            if addon_id not in upgrades:
                updated.append(entry)
                continue
            updated.append({**entry, "manifest": upgrades[addon_id]})
        backup = await addon_collection._push(updated, installed)
    return {
        "apply": apply,
        "checked": len(checks),
        "upgrade_count": len(upgrades),
        "applied": len(upgrades) if apply else 0,
        "backup": backup,
        "addons": checks,
    }


async def _casting_json(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> Any:
    http = await client()
    url = f"{desktop.STREAMING_SERVER_URL}/casting/{path.lstrip('/')}"
    try:
        response = await http.request(method, url, json=payload, timeout=60.0)
    except httpx.HTTPError as error:
        raise HttpError(f"Request to {url} failed: {error}") from error
    if response.status_code != 200:
        raise HttpError(f"HTTP {response.status_code} from {url}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as error:
        raise HttpError(f"Invalid JSON from {url}: {error}") from error


async def cast_devices() -> list[dict[str, Any]]:
    payload = await _casting_json("GET", "/")
    if not isinstance(payload, list):
        raise OperationsError("casting discovery returned no device list")
    return [
        {
            "id": device["id"],
            "name": device["name"],
            "type": "dlna" if device["type"] == "tv" else device["type"],
            "facility": device.get("facility"),
            "host": device.get("host"),
        }
        for device in payload
        if isinstance(device, dict) and device.get("type") in _CAST_TYPES
    ]


async def _cast_device(selector: str) -> dict[str, Any]:
    devices = await cast_devices()
    if not selector.strip():
        if len(devices) == 1:
            return devices[0]
        raise OperationsError("device is required when discovery finds multiple cast targets")
    needle = selector.strip().lower()
    matches = [
        device
        for device in devices
        if device["id"].lower() == needle or device["name"].lower() == needle
    ]
    if not matches:
        raise OperationsError(f"no Chromecast or DLNA device matches {selector!r}")
    if len(matches) > 1:
        raise OperationsError(f"multiple cast devices match {selector!r}; use the device id")
    return matches[0]


async def _cast_player(device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = await _casting_json(
        "POST", f"{quote(device_id, safe='')}/player", payload
    )
    if not isinstance(result, dict):
        raise OperationsError(f"cast player returned an error: {result}")
    return result


def _cast_status(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in ("state", "paused", "time", "length", "volume", "audio", "audioTrack")
        if key in result
    }


async def cast_play(
    device: str,
    stream_url: str = "",
    info_hash: str = "",
    file_index: int = 0,
    resume_position_ms: int = 0,
) -> dict[str, Any]:
    target = await _cast_device(device)
    if info_hash:
        normalized_hash = info_hash.strip().lower()
        if not _INFO_HASH_RE.fullmatch(normalized_hash):
            raise OperationsError("info_hash must be a 40-character hexadecimal hash")
        if file_index < 0:
            raise OperationsError("file_index cannot be negative")
        source = f"{desktop.STREAMING_SERVER_URL}/{normalized_hash}/{file_index}"
    else:
        source = stream_url.strip()
        if not source.startswith(("http://", "https://")):
            raise OperationsError("stream_url must be an http(s) URL")
    status = await _cast_player(target["id"], {"source": source})
    if resume_position_ms:
        if resume_position_ms < 0:
            raise OperationsError("resume_position_ms cannot be negative")
        await asyncio.sleep(5)
        status = await _cast_player(
            target["id"], {"time": resume_position_ms}
        )
    return {
        "device": target,
        "source": "torrent" if info_hash else "url",
        "resume_position_ms": resume_position_ms,
        "status": _cast_status(status),
    }


async def cast_control(device: str, action: str, value: float = 0) -> dict[str, Any]:
    target = await _cast_device(device)
    if action == "status":
        payload = {}
    elif action == "pause":
        payload = {"paused": True}
    elif action == "resume":
        payload = {"paused": False}
    elif action == "stop":
        payload = {"stop": True}
    elif action == "seek":
        if value < 0:
            raise OperationsError("seek value is milliseconds and cannot be negative")
        payload = {"time": int(value)}
    elif action == "volume":
        if not 0 <= value <= 1:
            raise OperationsError("volume must be between 0 and 1")
        payload = {"volume": value}
    else:
        raise OperationsError(
            "action must be status, pause, resume, stop, seek or volume"
        )
    status = await _cast_player(target["id"], payload)
    return {"device": target, "action": action, "status": _cast_status(status)}


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_addon_health_check() -> str:
        """Check every installed addon's manifest reachability and latency."""
        try:
            checks = await addon_health()
        except (api.ApiError, addon_collection.CollectionError) as error:
            return json.dumps({"ok": False, "error": str(error)})
        healthy = sum(check["healthy"] for check in checks)
        return json.dumps(
            {
                "ok": True,
                "checked": len(checks),
                "healthy": healthy,
                "unhealthy": len(checks) - healthy,
                "addons": checks,
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_check_addon_upgrades(apply: bool = False) -> str:
        """Compare installed addon versions with live manifests and optionally apply upgrades.

        Leave apply false to inspect. Set apply true to reinstall every newer
        manifest in one guarded account collection update.
        """
        try:
            result = await addon_upgrades(apply)
        except (
            api.ApiError,
            addon_collection.CollectionError,
            OperationsError,
        ) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_tv_pair(
        pairing_port: int,
        pairing_code: str,
        host: str = "",
        connect_port: int = 0,
    ) -> str:
        """Pair adb with an Android 11+ TV using its six-digit wireless code.

        pairing_port is the port shown beside the pairing code. host defaults to
        ANDROID_TV_HOST. Pass connect_port when the TV shows a separate wireless
        debugging port and the tool will connect after pairing.
        """
        try:
            target = tv.AndroidTV(
                host or tv.settings.android_tv_host,
                connect_port or tv.settings.android_tv_port,
                tv.settings.adb_path,
            )
            paired_target = await target.pair(pairing_port, pairing_code)
            connected_target = None
            if connect_port:
                await target.connect()
                connected_target = target.target
        except (tv.AdbError, tv.TVError) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps(
            {
                "ok": True,
                "paired": paired_target,
                "connected": connected_target,
            }
        )

    @mcp.tool()
    async def stremio_cast_devices() -> str:
        """Discover Chromecast and DLNA renderers through Stremio desktop."""
        try:
            devices = await cast_devices()
        except (HttpError, OperationsError, KeyError) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps(
            {"ok": True, "count": len(devices), "devices": devices},
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_cast_play(
        device: str,
        stream_url: str = "",
        info_hash: str = "",
        file_index: int = 0,
        resume_position_ms: int = 0,
    ) -> str:
        """Play an HTTP stream or local torrent engine file on Chromecast or DLNA.

        device accepts a discovered id or exact name. Pass stream_url, or pass
        info_hash and file_index for a torrent active in Stremio desktop.
        """
        try:
            result = await cast_play(
                device, stream_url, info_hash, file_index, resume_position_ms
            )
        except (HttpError, OperationsError, KeyError, TypeError) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_cast_control(
        device: str, action: str, value: float = 0
    ) -> str:
        """Read or control cast playback with status, pause, resume, stop, seek or volume."""
        try:
            result = await cast_control(device, action, value)
        except (HttpError, OperationsError, KeyError, TypeError) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)
