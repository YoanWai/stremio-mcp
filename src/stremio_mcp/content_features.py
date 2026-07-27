from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import webbrowser
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from . import account, addon_collection, addons, api, desktop, subtitle_addon
from .net import HttpError, get_json

CALENDAR_IDS_LIMIT = 100
OPENSUBTITLES_URL = "https://opensubtitles-v3.strem.io"
_OPENSUB_HASH_RE = re.compile(r"[0-9a-f]{16}")


class ContentError(RuntimeError):
    pass


def _resource_names(manifest: dict[str, Any]) -> set[str]:
    return {
        resource if isinstance(resource, str) else resource["name"]
        for resource in manifest.get("resources") or []
    }


def _trakt_linked(trakt: Any, now: float | None = None) -> bool:
    if not isinstance(trakt, dict):
        return False
    required = ("created_at", "expires_in", "access_token")
    if not all(trakt.get(field) for field in required):
        return False
    return (now if now is not None else time.time()) < trakt["created_at"] + trakt["expires_in"]


async def _install_manifest(manifest_url: str, manifest: dict[str, Any]) -> dict[str, Any]:
    addon_id = manifest.get("id")
    if not addon_id:
        raise ContentError(f"{manifest_url} returned no addon id")
    current = await addon_collection.fetch()
    existing_index = next(
        (
            index
            for index, entry in enumerate(current)
            if addon_collection.descriptor_id(entry) == addon_id
        ),
        None,
    )
    flags = (
        current[existing_index].get("flags")
        if existing_index is not None
        else {"official": False, "protected": False}
    )
    descriptor = {
        "transportUrl": manifest_url,
        "transportName": "http",
        "manifest": manifest,
        "flags": flags,
    }
    if existing_index is not None:
        previous = current[existing_index]
        if (
            previous["transportUrl"] == manifest_url
            and previous["manifest"].get("version") == manifest.get("version")
        ):
            return {"action": "current", "addon": addon_collection._summarize(previous)}
        updated = [*current]
        updated[existing_index] = descriptor
    else:
        updated = [*current, descriptor]
    backup = await addon_collection._push(updated, current)
    return {
        "action": "upgraded" if existing_index is not None else "installed",
        "addon": addon_collection._summarize(descriptor),
        "backup": backup,
    }


async def trakt_sync(open_auth: bool = True) -> dict[str, Any]:
    data = await api.authed("getUser")
    user = data.get("result")
    if not isinstance(user, dict) or not isinstance(user.get("_id"), str):
        raise ContentError("getUser returned no account id")
    user_id = user["_id"]
    if not _trakt_linked(user.get("trakt")):
        authorization_url = f"https://www.strem.io/trakt/auth/{quote(user_id, safe='')}"
        opened = False
        if open_auth:
            opened = await asyncio.to_thread(webbrowser.open, authorization_url)
            if not opened:
                raise ContentError(f"could not open {authorization_url}")
        return {
            "linked": False,
            "authorization_url": authorization_url,
            "authorization_opened": opened,
            "next": "finish Trakt authorization, then run this tool again",
        }

    manifest_url = (
        f"https://www.strem.io/trakt/addon/{quote(user_id, safe='')}/manifest.json"
    )
    manifest = await get_json(manifest_url)
    installed = await _install_manifest(manifest_url, manifest)
    return {"linked": True, "manifest_url": manifest_url, **installed}


def _catalog_extra(catalog: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next(
        (
            extra
            for extra in catalog.get("extra") or []
            if isinstance(extra, dict) and extra.get("name") == name
        ),
        None,
    )


def _calendar_requests(
    installed: list[dict[str, Any]], library_items: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any], list[str]]]:
    series = [
        item
        for item in library_items
        if item.get("type") == "series" and not item.get("removed") and not item.get("temp")
    ]
    series.sort(key=lambda item: item.get("_mtime") or "", reverse=True)
    recent = series[:CALENDAR_IDS_LIMIT]
    requests = []
    for entry in installed:
        manifest = entry["manifest"]
        if "catalog" not in _resource_names(manifest) or "series" not in manifest.get("types", []):
            continue
        prefixes = manifest.get("idPrefixes")
        supported_ids = [
            item["_id"]
            for item in recent
            if not prefixes or any(item["_id"].startswith(prefix) for prefix in prefixes)
        ]
        if not supported_ids:
            continue
        for catalog in manifest.get("catalogs") or []:
            if not isinstance(catalog, dict) or catalog.get("type") != "series":
                continue
            extra = _catalog_extra(catalog, "calendarVideosIds")
            if extra is None:
                continue
            options_limit = extra.get("optionsLimit", CALENDAR_IDS_LIMIT)
            limit = min(CALENDAR_IDS_LIMIT, options_limit)
            requests.append((entry, catalog, sorted(supported_ids[:limit])))
    return requests


def _released_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def upcoming_episodes(days: int = 90, limit: int = 50) -> dict[str, Any]:
    if days < 1:
        raise ContentError("days must be at least 1")
    installed, library_items = await asyncio.gather(
        addon_collection.fetch(), account.get_items()
    )
    requests = _calendar_requests(installed, library_items)
    if not requests:
        return {"days": days, "count": 0, "episodes": [], "failed_addons": []}

    async def fetch_calendar(entry, catalog, ids):
        base = addons.normalize_addon_base(entry["transportUrl"])
        extra = f"calendarVideosIds={quote(','.join(ids), safe='')}"
        url = (
            f"{base}/catalog/{quote(catalog['type'], safe='')}/"
            f"{quote(catalog['id'], safe='')}/{extra}.json"
        )
        try:
            payload = await get_json(url)
            metas = payload.get("metasDetailed")
            if not isinstance(metas, list):
                raise HttpError(f"{url} returned no 'metasDetailed' list")
            return entry, metas, None
        except HttpError as error:
            return entry, None, str(error)

    outcomes = await asyncio.gather(
        *(fetch_calendar(entry, catalog, ids) for entry, catalog, ids in requests)
    )
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    episodes: dict[str, dict[str, Any]] = {}
    failures = []
    for entry, metas, error in outcomes:
        addon_name = entry["manifest"].get("name") or entry["manifest"]["id"]
        if error:
            failures.append({"addon": addon_name, "error": error})
            continue
        for meta in metas:
            for video in meta.get("videos") or []:
                released = video.get("released")
                if not released:
                    continue
                release_time = _released_at(released)
                if now <= release_time <= end:
                    episodes.setdefault(
                        video["id"],
                        {
                            "addon": addon_name,
                            "series_id": meta["id"],
                            "series_name": meta["name"],
                            "video_id": video["id"],
                            "season": video.get("season"),
                            "episode": video.get("episode") or video.get("number"),
                            "name": video.get("name"),
                            "released": released,
                        },
                    )
    selected = sorted(episodes.values(), key=lambda episode: episode["released"])[
        : max(1, limit)
    ]
    return {
        "days": days,
        "count": len(selected),
        "episodes": selected,
        "failed_addons": failures,
    }


def _sorted_series_videos(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        videos,
        key=lambda video: (
            (video.get("season") or 0) if (video.get("season") or 0) != 0 else 2**32,
            video.get("episode") or video.get("number") or 0,
        ),
    )


def _watched_flags(serialized: str, video_ids: list[str]) -> list[bool]:
    if not serialized:
        return [False] * len(video_ids)
    try:
        anchor_video, anchor_length_raw, encoded = serialized.rsplit(":", 2)
        anchor_length = int(anchor_length_raw)
        values = zlib.decompress(base64.b64decode(encoded, validate=True))
    except (ValueError, zlib.error) as error:
        raise ContentError(f"invalid watched bitfield: {error}") from error
    if anchor_video not in video_ids:
        return [False] * len(video_ids)
    anchor_index = video_ids.index(anchor_video)
    offset = anchor_length - anchor_index - 1

    def bit(index: int) -> bool:
        if index < 0 or index >= len(video_ids) or index // 8 >= len(values):
            return False
        return bool((values[index // 8] >> (index % 8)) & 1)

    return [bit(index + offset) for index in range(len(video_ids))]


async def next_unwatched_episode(
    imdb_id: str, include_unreleased: bool = False
) -> dict[str, Any]:
    library_items, payload = await asyncio.gather(
        account.get_items(),
        get_json(f"{addons.CINEMETA_BASE}/meta/series/{quote(imdb_id, safe='')}.json"),
    )
    item = next((item for item in library_items if item.get("_id") == imdb_id), None)
    if item is None:
        raise ContentError(f"{imdb_id} has no library watch state")
    meta = payload.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("videos"), list):
        raise ContentError(f"Cinemeta returned no episode list for {imdb_id}")
    videos = _sorted_series_videos(meta["videos"])
    flags = _watched_flags(
        (item.get("state") or {}).get("watched") or "",
        [video["id"] for video in videos],
    )
    now = datetime.now(timezone.utc)
    episodes = [
        (video, watched)
        for video, watched in zip(videos, flags, strict=True)
        if (video.get("season") or 0) != 0
        and (
            include_unreleased
            or not video.get("released")
            or _released_at(video["released"]) <= now
        )
    ]
    next_episode = next((video for video, watched in episodes if not watched), None)
    return {
        "series_id": imdb_id,
        "series_name": meta.get("name") or item.get("name"),
        "watched_episodes": sum(watched for _, watched in episodes),
        "episode_count": len(episodes),
        "complete": next_episode is None,
        "next_episode": (
            {
                "video_id": next_episode["id"],
                "season": next_episode.get("season"),
                "episode": next_episode.get("episode") or next_episode.get("number"),
                "name": next_episode.get("name"),
                "released": next_episode.get("released"),
                "resume_position_ms": (
                    (item.get("state") or {}).get("timeOffset") or 0
                    if (item.get("state") or {}).get("video_id") == next_episode["id"]
                    else 0
                ),
            }
            if next_episode
            else None
        ),
    }


def _search_catalogs(
    installed: list[dict[str, Any]], content_type: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (entry, catalog)
        for entry in installed
        for catalog in entry["manifest"].get("catalogs") or []
        if isinstance(catalog, dict)
        and (not content_type or catalog.get("type") == content_type)
        and _catalog_extra(catalog, "search") is not None
        and "catalog" in _resource_names(entry["manifest"])
    ]


def _catalog_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "type": meta.get("type"),
        "year": meta.get("year") or meta.get("releaseInfo"),
        "poster": meta.get("poster"),
        "imdb_rating": meta.get("imdbRating"),
    }


async def cross_catalog_search(
    query: str, content_type: str = "", per_catalog: int = 10
) -> dict[str, Any]:
    if not query.strip():
        raise ContentError("query cannot be empty")
    installed = await addon_collection.fetch()
    catalogs = _search_catalogs(installed, content_type)

    async def search_catalog(entry, catalog):
        base = addons.normalize_addon_base(entry["transportUrl"])
        url = (
            f"{base}/catalog/{quote(catalog['type'], safe='')}/"
            f"{quote(catalog['id'], safe='')}/search={quote(query, safe='')}.json"
        )
        try:
            payload = await get_json(url)
            metas = payload.get("metas")
            if not isinstance(metas, list):
                raise HttpError(f"{url} returned no 'metas' list")
            return entry, catalog, metas, None
        except HttpError as error:
            return entry, catalog, None, str(error)

    outcomes = await asyncio.gather(*(search_catalog(*catalog) for catalog in catalogs))
    results = []
    failures = []
    for entry, catalog, metas, error in outcomes:
        addon_name = entry["manifest"].get("name") or entry["manifest"]["id"]
        if error:
            failures.append(
                {"addon": addon_name, "catalog": catalog["id"], "error": error}
            )
            continue
        results.extend(
            {
                "addon": addon_name,
                "catalog": catalog["id"],
                **_catalog_meta(meta),
            }
            for meta in metas[: max(1, per_catalog)]
        )
    return {
        "query": query,
        "content_type": content_type or None,
        "catalogs_queried": len(catalogs),
        "count": len(results),
        "results": results,
        "failed_catalogs": failures,
    }


async def _opensubtitle_video_params(
    video_url: str,
    video_hash: str,
    video_size: int,
    filename: str,
) -> tuple[str, int, str]:
    if video_hash:
        normalized = video_hash.strip().lower()
        if not _OPENSUB_HASH_RE.fullmatch(normalized):
            raise ContentError("video_hash must be a 16-character OpenSubtitles hash")
        if video_size < 1:
            raise ContentError("video_size is required with video_hash")
        return normalized, video_size, filename
    if not video_url:
        raise ContentError("pass video_url, or pass both video_hash and video_size")
    payload = await get_json(
        f"{desktop.STREAMING_SERVER_URL}/opensubHash?"
        f"videoUrl={quote(video_url, safe='')}"
    )
    if payload.get("error"):
        raise ContentError(f"streaming server could not hash the video: {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict) or not result.get("hash") or not result.get("size"):
        raise ContentError("streaming server returned no video hash and size")
    inferred_filename = unquote(Path(urlparse(video_url).path).name)
    return result["hash"], result["size"], filename or inferred_filename


async def auto_fetch_subtitle(
    imdb_id: str,
    language: str = "eng",
    season: int = 0,
    episode: int = 0,
    video_url: str = "",
    video_hash: str = "",
    video_size: int = 0,
    filename: str = "",
    result_index: int = 0,
) -> dict[str, Any]:
    if bool(season) != bool(episode):
        raise ContentError("season and episode must be supplied together")
    resolved_hash, resolved_size, resolved_filename = await _opensubtitle_video_params(
        video_url, video_hash, video_size, filename
    )
    content_type = "series" if season and episode else "movie"
    video_id = (
        f"{imdb_id}:{season}:{episode}" if content_type == "series" else imdb_id
    )
    extras = [
        ("videoHash", resolved_hash),
        ("videoSize", str(resolved_size)),
    ]
    if resolved_filename:
        extras.append(("filename", resolved_filename))
    encoded_extra = "&".join(
        f"{quote(name, safe='')}={quote(value, safe='')}" for name, value in extras
    )
    url = (
        f"{OPENSUBTITLES_URL}/subtitles/{content_type}/{quote(video_id, safe='')}/"
        f"{encoded_extra}.json"
    )
    payload = await get_json(url)
    subtitles = payload.get("subtitles")
    if not isinstance(subtitles, list):
        raise ContentError(f"{url} returned no 'subtitles' list")
    matches = [subtitle for subtitle in subtitles if subtitle.get("lang") == language]
    if not matches:
        raise ContentError(f"OpenSubtitles returned no {language} subtitle for {video_id}")
    if result_index < 0 or result_index >= len(matches):
        raise ContentError(
            f"result_index must be between 0 and {len(matches) - 1} for {language}"
        )
    selected = matches[result_index]
    added = await subtitle_addon.add_subtitle_url(
        imdb_id=imdb_id,
        subtitle_url=selected["url"],
        language=language,
        season=season,
        episode=episode,
        label=selected.get("label") or f"{language.upper()} OpenSubtitles",
    )
    return {
        "provider": "OpenSubtitles v3",
        "provider_subtitle_id": selected.get("id"),
        "video_hash": resolved_hash,
        "video_size": resolved_size,
        "matching_subtitles": len(matches),
        **added,
    }


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_trakt_sync(open_auth: bool = True) -> str:
        """Link Trakt in the browser, then install or refresh Stremio's Trakt addon.

        On the first call, the account-specific authorization page opens. After
        authorizing Trakt, run the tool again to sync the first-party addon.
        """
        try:
            result = await trakt_sync(open_auth)
        except (ContentError, HttpError, api.ApiError, addon_collection.CollectionError) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_upcoming_episodes(days: int = 90, limit: int = 50) -> str:
        """List episodes releasing soon for series in the account library."""
        try:
            result = await upcoming_episodes(days, limit)
        except (
            ContentError,
            HttpError,
            api.ApiError,
            addon_collection.CollectionError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_next_unwatched_episode(
        imdb_id: str, include_unreleased: bool = False
    ) -> str:
        """Resolve the first unwatched regular episode using synced library history."""
        try:
            result = await next_unwatched_episode(imdb_id, include_unreleased)
        except (
            ContentError,
            HttpError,
            api.ApiError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_search_all_catalogs(
        query: str, content_type: str = "", per_catalog: int = 10
    ) -> str:
        """Search every installed addon catalog that declares search support."""
        try:
            result = await cross_catalog_search(query, content_type, per_catalog)
        except (
            ContentError,
            HttpError,
            api.ApiError,
            addon_collection.CollectionError,
            KeyError,
            TypeError,
        ) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_auto_fetch_subtitle(
        imdb_id: str,
        language: str = "eng",
        season: int = 0,
        episode: int = 0,
        video_url: str = "",
        video_hash: str = "",
        video_size: int = 0,
        filename: str = "",
        result_index: int = 0,
    ) -> str:
        """Fetch a hash-matched OpenSubtitles result and serve it through add_subtitle.

        Pass a playable video_url while Stremio desktop is running so its server
        calculates the hash, or provide video_hash and video_size directly.
        """
        try:
            result = await auto_fetch_subtitle(
                imdb_id,
                language,
                season,
                episode,
                video_url,
                video_hash,
                video_size,
                filename,
                result_index,
            )
        except (
            ContentError,
            HttpError,
            api.ApiError,
            addon_collection.CollectionError,
            subtitle_addon.SubtitleError,
            OSError,
            KeyError,
            TypeError,
        ) as error:
            return json.dumps({"ok": False, "error": str(error)})
        return json.dumps({"ok": True, **result}, ensure_ascii=False)
