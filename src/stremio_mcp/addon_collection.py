"""Manage the Stremio account's addon collection.

The collection lives on the account, so every change here reaches every signed-in
client: Stremio desktop on macOS, Windows and Linux, the Android TV app, mobile
and web. That makes this the cross-platform way to install addons on a laptop.

``addonCollectionSet`` replaces the whole collection in one shot, so every write
goes through :func:`_push`, which merges against the live collection, refuses to
drop protected addons, and snapshots the previous state to disk first.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import api
from .config import settings
from .net import HttpError, get_bytes, get_json

OFFICIAL_DIRECTORY_URL = "https://api.strem.io/addonscollection.json"
DEFAULT_ADDONS_URL = (
    "https://raw.githubusercontent.com/Stremio/stremio-official-addons/master/index.json"
)

# Stremio refuses to run without these; never let a write remove them.
PROTECTED_IDS = frozenset({"com.linvo.cinemeta", "org.stremio.local"})


class CollectionError(RuntimeError):
    """Raised when the addon collection cannot be read or written safely."""


def normalize_manifest_url(url: str) -> str:
    """Turn any form a user might paste into a canonical manifest URL."""
    cleaned = url.strip()
    if cleaned.startswith("stremio://"):
        cleaned = "https://" + cleaned[len("stremio://") :]
    if not cleaned.startswith(("http://", "https://")):
        raise CollectionError(f"addon URL must be http(s) or stremio://, got {url!r}")
    cleaned = cleaned.rstrip("/")
    if not cleaned.endswith("/manifest.json") and not cleaned.endswith("/stremio/v1"):
        cleaned = f"{cleaned}/manifest.json"
    return cleaned


def is_valid_descriptor(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("manifest"), dict)
        and bool(entry["manifest"].get("id"))
        and bool(entry.get("transportUrl"))
    )


def descriptor_id(entry: dict[str, Any]) -> str:
    return entry["manifest"]["id"]


def _summarize(entry: dict[str, Any]) -> dict[str, Any]:
    manifest = entry.get("manifest") or {}
    flags = entry.get("flags") or {}
    return {
        "id": manifest.get("id"),
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "types": manifest.get("types"),
        "resources": [
            resource if isinstance(resource, str) else resource.get("name")
            for resource in manifest.get("resources") or []
        ],
        "transportUrl": entry.get("transportUrl"),
        "official": bool(flags.get("official")),
        "protected": bool(flags.get("protected")),
    }


async def fetch() -> list[dict[str, Any]]:
    """Read the account's addon collection, dropping entries the API cannot use."""
    data = await api.authed("addonCollectionGet", {"update": True})
    result = data.get("result") or {}
    addons = result.get("addons") if isinstance(result, dict) else None
    if not isinstance(addons, list):
        raise CollectionError("addonCollectionGet returned no addon list")
    return [entry for entry in addons if is_valid_descriptor(entry)]


def _snapshot(addons: list[dict[str, Any]]) -> str | None:
    path = settings.state_dir / "addon-backups"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path / f"addons-{stamp}.json"
    try:
        path.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(addons, indent=2), encoding="utf-8")
    except OSError:
        return None
    for stale in sorted(path.glob("addons-*.json"))[:-20]:
        stale.unlink(missing_ok=True)
    return str(target)


async def _push(addons: list[dict[str, Any]], previous: list[dict[str, Any]]) -> str | None:
    """Write a full collection after checking it keeps every protected addon."""
    usable = [entry for entry in addons if is_valid_descriptor(entry)]
    if not usable:
        raise CollectionError("refusing to write an empty addon collection")
    kept = {descriptor_id(entry) for entry in usable}
    lost = {descriptor_id(entry) for entry in previous if descriptor_id(entry) in PROTECTED_IDS}
    missing = lost - kept
    if missing:
        raise CollectionError(f"refusing to remove protected addons: {sorted(missing)}")
    backup = _snapshot(previous)
    await api.authed("addonCollectionSet", {"addons": usable})
    return backup


async def fetch_defaults() -> list[dict[str, Any]]:
    """The seven addons a fresh Stremio profile ships with."""
    raw = await get_bytes(DEFAULT_ADDONS_URL)
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise CollectionError("official defaults payload was not a list")
    return [entry for entry in entries if is_valid_descriptor(entry)]


async def fetch_directory() -> list[dict[str, Any]]:
    """The community addon directory Stremio itself shows under Addons."""
    raw = await get_bytes(OFFICIAL_DIRECTORY_URL)
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise CollectionError("addon directory payload was not a list")
    return [entry for entry in entries if is_valid_descriptor(entry)]


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_list_addons() -> str:
        """List the addons installed on the Stremio account.

        These are the addons every signed-in device sees, including Stremio
        desktop on macOS, Windows and Linux. Order matters: for a given stream
        request Stremio queries addons top to bottom.
        """
        try:
            addons = await fetch()
        except (api.ApiError, CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {
                "ok": True,
                "count": len(addons),
                "addons": [
                    {"position": index, **_summarize(entry)}
                    for index, entry in enumerate(addons)
                ],
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_search_addons(query: str = "", limit: int = 20) -> str:
        """Search the official Stremio addon directory for addons to install.

        Leave query empty to list the directory. Pass a returned transportUrl to
        stremio_install_addon. Addons flagged configuration_required must be
        configured in a browser first, which produces the real manifest URL.
        """
        try:
            entries = await fetch_directory()
        except (HttpError, ValueError, CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        needle = query.strip().lower()
        if needle:
            entries = [
                entry
                for entry in entries
                if needle in json.dumps(entry.get("manifest"), ensure_ascii=False).lower()
            ]
        results = []
        for entry in entries[: max(1, limit)]:
            manifest = entry["manifest"]
            hints = manifest.get("behaviorHints") or {}
            results.append(
                {
                    **_summarize(entry),
                    "description": manifest.get("description"),
                    "configuration_required": bool(hints.get("configurationRequired")),
                    "adult": bool(hints.get("adult")),
                }
            )
        return json.dumps(
            {"ok": True, "query": query or None, "count": len(results), "addons": results},
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_install_addon(manifest_url: str, position: int = -1) -> str:
        """Install an addon onto the Stremio account by its manifest URL.

        It shows up on every signed-in device: Stremio desktop on macOS, Windows
        and Linux, Android TV, mobile and web. Reinstalling an already-present
        addon upgrades it in place. position places the addon in the query order
        (0 is queried first); -1 appends.
        """
        try:
            url = normalize_manifest_url(manifest_url)
            manifest = await get_json(url)
        except (CollectionError, HttpError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        if not manifest.get("id"):
            return json.dumps({"ok": False, "error": f"{url} is not a Stremio manifest (no id)"})

        try:
            current = await fetch()
        except (api.ApiError, CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        addon_id = manifest["id"]
        existing = next((e for e in current if descriptor_id(e) == addon_id), None)
        flags = (existing or {}).get("flags") or {"official": False, "protected": False}
        descriptor = {"transportUrl": url, "transportName": "http", "manifest": manifest, "flags": flags}

        remaining = [entry for entry in current if descriptor_id(entry) != addon_id]
        index = len(remaining) if position < 0 else min(position, len(remaining))
        updated = remaining[:index] + [descriptor] + remaining[index:]

        try:
            backup = await _push(updated, current)
        except (api.ApiError, CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {
                "ok": True,
                "action": "upgraded" if existing else "installed",
                "addon": _summarize(descriptor),
                "position": index,
                "total": len(updated),
                "backup": backup,
                "note": "Synced to the account. Restart or refresh Stremio on a device to pick it up.",
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_uninstall_addon(addon: str) -> str:
        """Remove an addon from the account by its manifest URL or its addon id."""
        try:
            current = await fetch()
        except (api.ApiError, CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        needle = addon.strip().rstrip("/")
        matches = [
            entry
            for entry in current
            if descriptor_id(entry) == needle
            or entry.get("transportUrl", "").rstrip("/") == needle
            or entry.get("transportUrl", "").rstrip("/").removesuffix("/manifest.json") == needle
        ]
        if not matches:
            return json.dumps({"ok": False, "error": f"no installed addon matches {addon!r}"})
        target_ids = {descriptor_id(entry) for entry in matches}
        blocked = target_ids & PROTECTED_IDS
        if blocked:
            return json.dumps({"ok": False, "error": f"{sorted(blocked)} is protected by Stremio"})
        remaining = [entry for entry in current if descriptor_id(entry) not in target_ids]
        try:
            backup = await _push(remaining, current)
        except (api.ApiError, CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {
                "ok": True,
                "removed": [_summarize(entry) for entry in matches],
                "total": len(remaining),
                "backup": backup,
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_reorder_addons(addon_ids: list[str]) -> str:
        """Set the order Stremio queries addons in, highest priority first.

        Pass the addon ids you care about; anything you leave out keeps its
        relative order behind them. Stream results follow this order, so moving a
        preferred stream provider to the front changes what plays first.
        """
        try:
            current = await fetch()
        except (api.ApiError, CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        by_id = {descriptor_id(entry): entry for entry in current}
        unknown = [addon_id for addon_id in addon_ids if addon_id not in by_id]
        if unknown:
            return json.dumps({"ok": False, "error": f"not installed: {unknown}"})
        front = [by_id[addon_id] for addon_id in addon_ids]
        back = [entry for entry in current if descriptor_id(entry) not in set(addon_ids)]
        updated = front + back
        try:
            backup = await _push(updated, current)
        except (api.ApiError, CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {
                "ok": True,
                "order": [descriptor_id(entry) for entry in updated],
                "backup": backup,
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_restore_default_addons(keep_installed: bool = True) -> str:
        """Reinstall Stremio's seven default addons onto the account.

        Use this when the addon list is empty or broken. keep_installed keeps any
        other addon already on the account and only fills in the missing
        defaults; pass false to reset the account to a stock addon list.
        """
        try:
            defaults = await fetch_defaults()
            current = await fetch()
        except (api.ApiError, CollectionError, HttpError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        default_ids = {descriptor_id(entry) for entry in defaults}
        if keep_installed:
            extras = [entry for entry in current if descriptor_id(entry) not in default_ids]
            updated = defaults + extras
        else:
            updated = defaults
        try:
            backup = await _push(updated, current)
        except (api.ApiError, CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {
                "ok": True,
                "before": len(current),
                "after": len(updated),
                "addons": [_summarize(entry) for entry in updated],
                "backup": backup,
            },
            ensure_ascii=False,
        )
