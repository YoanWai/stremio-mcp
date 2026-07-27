"""Inspect, open and complete browser-based addon configuration flows."""

from __future__ import annotations

import asyncio
import json
import platform
import shutil
from html.parser import HTMLParser
from typing import Any

import httpx

from . import addon_collection, api
from .net import HttpError, client, get_json

MAX_CONFIGURATION_FIELDS = 100


class ConfigurationError(RuntimeError):
    """Raised when an addon configuration flow cannot be resolved or opened."""


class ConfigurationPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.fields: list[dict[str, Any]] = []
        self.labels: dict[str, list[str]] = {}
        self._in_title = False
        self._label_for: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
            return
        if tag == "label":
            self._label_for = values.get("for")
            return
        if tag not in {"input", "select", "textarea"}:
            return
        field_type = values.get("type", "text") if tag == "input" else tag
        if field_type in {"hidden", "submit", "button", "reset"}:
            return
        key = values.get("name") or values.get("id")
        if not key:
            return
        self.fields.append(
            {
                "key": key,
                "type": field_type,
                "placeholder": values.get("placeholder"),
                "required": "required" in values,
            }
        )

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "label":
            self._label_for = None

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        if self._label_for:
            self.labels.setdefault(self._label_for, []).append(text)

    def result(self) -> dict[str, Any]:
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for field in self.fields:
            identity = (field["key"], field["type"])
            if identity in seen:
                continue
            seen.add(identity)
            label = " ".join(self.labels.get(field["key"], [])) or None
            unique.append({**field, "label": label})
        return {
            "page_title": " ".join(self.title_parts) or None,
            "field_count": len(unique),
            "fields": unique[:MAX_CONFIGURATION_FIELDS],
            "fields_truncated": len(unique) > MAX_CONFIGURATION_FIELDS,
        }


def configuration_url(manifest_url: str) -> str:
    base = manifest_url.strip().rstrip("/")
    if base.endswith("/manifest.json"):
        base = base[: -len("/manifest.json")]
    return f"{base}/configure"


def resolve_directory_addon(
    entries: list[dict[str, Any]], addon: str
) -> dict[str, Any]:
    needle = addon.strip().rstrip("/").lower()

    def values(entry: dict[str, Any]) -> tuple[str, str, str]:
        manifest = entry["manifest"]
        return (
            str(manifest.get("id") or "").lower(),
            str(manifest.get("name") or "").lower(),
            str(entry.get("transportUrl") or "").rstrip("/").lower(),
        )

    exact = [entry for entry in entries if needle in values(entry)]
    matches = exact or [
        entry
        for entry in entries
        if any(needle in value for value in values(entry))
    ]
    if not matches:
        raise ConfigurationError(f"no directory addon matches {addon!r}")
    if len(matches) > 1:
        choices = [
            {
                "id": entry["manifest"].get("id"),
                "name": entry["manifest"].get("name"),
                "transportUrl": entry.get("transportUrl"),
            }
            for entry in matches[:10]
        ]
        raise ConfigurationError(f"addon query is ambiguous; matches: {choices}")
    return matches[0]


async def inspect_configuration_page(url: str) -> dict[str, Any]:
    http = await client()
    try:
        response = await http.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise HttpError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise HttpError(f"HTTP {response.status_code} from {url}")
    parser = ConfigurationPageParser()
    parser.feed(response.text)
    return {"configuration_url": str(response.url), **parser.result()}


async def open_browser(url: str) -> None:
    system = platform.system()
    if system == "Darwin":
        command = ["open", url]
    elif system == "Windows":
        command = ["cmd", "/c", "start", "", url]
    else:
        opener = shutil.which("xdg-open")
        if opener is None:
            raise ConfigurationError("xdg-open is not installed")
        command = [opener, url]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise ConfigurationError(detail or f"could not open {url}")


async def start_configuration(addon: str, should_open_browser: bool) -> dict[str, Any]:
    entries = await addon_collection.fetch_directory()
    entry = resolve_directory_addon(entries, addon)
    manifest = entry["manifest"]
    hints = manifest.get("behaviorHints") or {}
    page = await inspect_configuration_page(configuration_url(entry["transportUrl"]))
    if should_open_browser:
        await open_browser(page["configuration_url"])
    return {
        "ok": True,
        "stage": "configuration",
        "addon": {
            "id": manifest["id"],
            "name": manifest.get("name"),
            "version": manifest.get("version"),
            "description": manifest.get("description"),
            "transportUrl": entry["transportUrl"],
        },
        "configuration_required": bool(hints.get("configurationRequired")),
        "configurable": bool(hints.get("configurable")),
        **page,
        "browser_opened": should_open_browser,
        "next_step": (
            "Complete the configuration page, copy its manifest URL, then call this tool "
            "again with configured_manifest_url."
        ),
    }


async def complete_configuration(
    addon: str,
    configured_manifest_url: str,
    position: int,
) -> dict[str, Any]:
    entries = await addon_collection.fetch_directory()
    entry = resolve_directory_addon(entries, addon)
    expected_id = entry["manifest"]["id"]
    url = addon_collection.normalize_manifest_url(configured_manifest_url)
    manifest = await get_json(url)
    if manifest.get("id") != expected_id:
        raise ConfigurationError(
            f"configured manifest id {manifest.get('id')!r} does not match {expected_id!r}"
        )
    hints = manifest.get("behaviorHints") or {}
    if hints.get("configurationRequired"):
        raise ConfigurationError(
            "the manifest still requires configuration; use the URL produced after saving the form"
        )
    result = await addon_collection.install(url, position, expected_id)
    return {"stage": "installed", **result}


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_configure_addon(
        addon: str,
        configured_manifest_url: str = "",
        position: int = -1,
        open_configuration_page: bool = True,
    ) -> str:
        """Run a configurable addon's browser setup and install its result.

        First call with a directory addon id, name or transport URL. The tool
        reports the page fields and opens the configuration page. Complete that
        page, then call again with the configured manifest URL it produces.
        """
        try:
            if configured_manifest_url:
                result = await complete_configuration(
                    addon,
                    configured_manifest_url,
                    position,
                )
            else:
                result = await start_configuration(addon, open_configuration_page)
        except (
            ConfigurationError,
            addon_collection.CollectionError,
            api.ApiError,
            HttpError,
            ValueError,
        ) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(result, ensure_ascii=False)
