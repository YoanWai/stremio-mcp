"""Save and apply named Stremio addon collections."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import addon_collection, api
from .config import settings

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")


class ProfileError(RuntimeError):
    """Raised when an addon profile name or file is invalid."""


def profiles_dir() -> Path:
    return settings.state_dir / "addon-profiles"


def profile_path(name: str) -> Path:
    cleaned = name.strip()
    if not _PROFILE_NAME_RE.fullmatch(cleaned) or cleaned in {".", ".."}:
        raise ProfileError(
            "profile name must start with a letter or number and contain only "
            "letters, numbers, spaces, periods, underscores or hyphens"
        )
    return profiles_dir() / f"{cleaned}.json"


def read_profile(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"could not read addon profile {path.stem!r}: {exc}") from exc
    if not isinstance(payload, list):
        raise ProfileError(f"addon profile {path.stem!r} is not an addon list")
    invalid = [
        index
        for index, entry in enumerate(payload)
        if not addon_collection.is_valid_descriptor(entry)
    ]
    if invalid:
        raise ProfileError(f"addon profile {path.stem!r} has invalid entries at {invalid}")
    if not payload:
        raise ProfileError(f"addon profile {path.stem!r} is empty")
    return payload


def profile_summary(path: Path, addons: list[dict[str, Any]]) -> dict[str, Any]:
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return {
        "name": path.stem,
        "addon_count": len(addons),
        "addon_ids": [addon_collection.descriptor_id(entry) for entry in addons],
        "modified": modified,
    }


async def save_profile(name: str, overwrite: bool) -> dict[str, Any]:
    target = profile_path(name)
    if target.exists() and not overwrite:
        raise ProfileError(f"addon profile {target.stem!r} already exists")
    addons = await addon_collection.fetch()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(addons, indent=2, ensure_ascii=False), encoding="utf-8")
    return profile_summary(target, addons)


def list_profiles() -> list[dict[str, Any]]:
    directory = profiles_dir()
    paths = sorted(directory.glob("*.json"), key=lambda path: path.name.lower())
    return [profile_summary(path, read_profile(path)) for path in paths]


async def apply_profile(name: str) -> dict[str, Any]:
    target = profile_path(name)
    desired = read_profile(target)
    current = await addon_collection.fetch()
    backup = await addon_collection._push(desired, current)
    return {
        "ok": True,
        "profile": target.stem,
        "before": len(current),
        "after": len(desired),
        "addon_ids": [
            addon_collection.descriptor_id(entry) for entry in desired
        ],
        "backup": backup,
    }


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_save_addon_profile(name: str, overwrite: bool = False) -> str:
        """Save the account's current ordered addon collection under a name."""
        try:
            summary = await save_profile(name, overwrite)
        except (ProfileError, api.ApiError, addon_collection.CollectionError, OSError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, "profile": summary}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_list_addon_profiles() -> str:
        """List saved addon profiles and the ordered addon ids in each one."""
        try:
            profiles = list_profiles()
        except (ProfileError, OSError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {"ok": True, "count": len(profiles), "profiles": profiles},
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_apply_addon_profile(name: str) -> str:
        """Replace the account addon collection with a saved named profile.

        The protected-addon guard and automatic backup are applied before the
        account write.
        """
        try:
            result = await apply_profile(name)
        except (
            ProfileError,
            api.ApiError,
            addon_collection.CollectionError,
            OSError,
        ) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(result, ensure_ascii=False)
