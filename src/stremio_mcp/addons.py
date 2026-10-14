"""Content lookup over Stremio's public addon HTTP protocol.

Cinemeta supplies titles and metadata. Any addon that speaks the protocol can be
asked for streams directly, and :func:`stremio_find_streams` fans that question
out across every addon installed on the account at once.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

from . import api
from .net import HttpError, get_json

CINEMETA_BASE = "https://v3-cinemeta.strem.io"
MAX_STREAMS = 30
CONTENT_TYPES = ("movie", "series")


def normalize_addon_base(addon_url: str) -> str:
    base = addon_url.strip().rstrip("/")
    if base.endswith("/manifest.json"):
        base = base[: -len("/manifest.json")]
    if not base.startswith(("http://", "https://")):
        raise HttpError(f"addon_url must be an http(s) URL, got {addon_url!r}")
    return base


def build_video_id(imdb_id: str, content_type: str, season: int, episode: int) -> str:
    if content_type != "series":
        return imdb_id
    if not (season and episode):
        raise HttpError("season and episode are required for series")
    return f"{imdb_id}:{season}:{episode}"


def _slim_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "type": meta.get("type"),
        "year": meta.get("year") or meta.get("releaseInfo"),
        "imdbRating": meta.get("imdbRating"),
        "genres": meta.get("genres") or meta.get("genre"),
    }


def _slim_stream(stream: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": stream.get("name"),
        "title": stream.get("title") or stream.get("description"),
        "url": stream.get("url"),
        "infoHash": stream.get("infoHash"),
        "fileIdx": stream.get("fileIdx"),
        "behaviorHints": stream.get("behaviorHints"),
    }


def _supports_streams(manifest: dict[str, Any], content_type: str, imdb_id: str) -> bool:
    """Whether asking this addon for a stream is worth a request."""
    resources = manifest.get("resources") or []
    names = {
        resource if isinstance(resource, str) else resource.get("name")
        for resource in resources
    }
    if "stream" not in names:
        return False
    types = manifest.get("types") or []
    if types and content_type not in types:
        return False
    prefixes = manifest.get("idPrefixes")
    if prefixes and not any(imdb_id.startswith(prefix) for prefix in prefixes):
        return False
    return True


async def fetch_streams(base: str, content_type: str, video_id: str) -> list[dict[str, Any]]:
    payload = await get_json(f"{base}/stream/{content_type}/{quote(video_id)}.json")
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise HttpError(f"{base} returned no 'streams' list")
    return [stream for stream in streams if isinstance(stream, dict)]


async def find_stream_candidates(
    imdb_id: str,
    content_type: str,
    video_id: str,
    per_addon: int = 10,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    from . import addon_collection

    installed = await addon_collection.fetch()
    providers = [
        entry
        for entry in installed
        if _supports_streams(entry["manifest"], content_type, imdb_id)
    ]

    async def ask(entry: dict[str, Any]) -> tuple[dict[str, Any], list | None, str | None]:
        base = normalize_addon_base(entry["transportUrl"])
        try:
            return entry, await fetch_streams(base, content_type, video_id), None
        except HttpError as exc:
            return entry, None, str(exc)

    outcomes = await asyncio.gather(*(ask(entry) for entry in providers))
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for entry, streams, error in outcomes:
        manifest = entry["manifest"]
        name = manifest.get("name") or manifest["id"]
        if error is not None:
            failures.append({"addon": name, "error": error})
            continue
        for stream in streams[: max(1, per_addon)]:
            candidates.append(
                {
                    "addon": name,
                    "addon_id": manifest["id"],
                    "transport_url": entry["transportUrl"],
                    "stream": stream,
                }
            )
    return candidates, failures, len(providers)


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_search(query: str, content_type: str = "movie", limit: int = 10) -> str:
        """Search movies or series by name through the public Cinemeta catalog."""
        if content_type not in CONTENT_TYPES:
            return json.dumps({"ok": False, "error": f"content_type must be one of {CONTENT_TYPES}"})
        url = f"{CINEMETA_BASE}/catalog/{content_type}/top/search={quote(query)}.json"
        try:
            payload = await get_json(url)
        except HttpError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        metas = payload.get("metas")
        if not isinstance(metas, list):
            return json.dumps({"ok": False, "error": f"Cinemeta returned no 'metas' list for {url}"})
        results = [_slim_meta(meta) for meta in metas[: max(1, limit)]]
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "content_type": content_type,
                "count": len(results),
                "results": results,
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_get_meta(imdb_id: str, content_type: str = "movie") -> str:
        """Get full metadata for a title by IMDb id, including the episode list for a series."""
        if content_type not in CONTENT_TYPES:
            return json.dumps({"ok": False, "error": f"content_type must be one of {CONTENT_TYPES}"})
        try:
            payload = await get_json(f"{CINEMETA_BASE}/meta/{content_type}/{quote(imdb_id)}.json")
        except HttpError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            return json.dumps({"ok": False, "error": f"Cinemeta has no metadata for {imdb_id}"})
        trimmed: dict[str, Any] = {
            **_slim_meta(meta),
            "description": meta.get("description"),
            "runtime": meta.get("runtime"),
            "cast": meta.get("cast"),
            "director": meta.get("director"),
        }
        videos = meta.get("videos")
        if content_type == "series" and isinstance(videos, list):
            trimmed["videos"] = [
                {
                    "id": video.get("id"),
                    "season": video.get("season"),
                    "episode": video.get("episode") or video.get("number"),
                    "name": video.get("name") or video.get("title"),
                    "released": video.get("released"),
                }
                for video in videos
            ]
        return json.dumps({"ok": True, **trimmed}, ensure_ascii=False)

    @mcp.tool()
    async def stremio_browse_catalog(
        content_type: str = "movie", genre: str = "", skip: int = 0
    ) -> str:
        """Browse the public Cinemeta top catalog, optionally filtered by genre."""
        if content_type not in CONTENT_TYPES:
            return json.dumps({"ok": False, "error": f"content_type must be one of {CONTENT_TYPES}"})
        extras = []
        if genre:
            extras.append(f"genre={quote(genre)}")
        if skip:
            extras.append(f"skip={skip}")
        suffix = f"/{'&'.join(extras)}" if extras else ""
        try:
            payload = await get_json(f"{CINEMETA_BASE}/catalog/{content_type}/top{suffix}.json")
        except HttpError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        metas = payload.get("metas")
        if not isinstance(metas, list):
            return json.dumps({"ok": False, "error": "Cinemeta returned no 'metas' list"})
        results = [_slim_meta(meta) for meta in metas]
        return json.dumps(
            {
                "ok": True,
                "content_type": content_type,
                "genre": genre or None,
                "skip": skip,
                "count": len(results),
                "results": results,
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_get_addon_manifest(addon_url: str) -> str:
        """Fetch a Stremio addon's manifest and summarize what it can do."""
        try:
            base = normalize_addon_base(addon_url)
            manifest = await get_json(f"{base}/manifest.json")
        except HttpError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {
                "ok": True,
                "addon_url": base,
                "id": manifest.get("id"),
                "name": manifest.get("name"),
                "version": manifest.get("version"),
                "description": manifest.get("description"),
                "resources": manifest.get("resources"),
                "types": manifest.get("types"),
                "idPrefixes": manifest.get("idPrefixes"),
                "behaviorHints": manifest.get("behaviorHints"),
                "catalogs": [
                    {"type": catalog.get("type"), "id": catalog.get("id"), "name": catalog.get("name")}
                    for catalog in manifest.get("catalogs") or []
                    if isinstance(catalog, dict)
                ],
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_get_streams(
        addon_url: str,
        imdb_id: str,
        content_type: str = "movie",
        season: int = 0,
        episode: int = 0,
    ) -> str:
        """Ask one specific addon for the streams it has for a movie or episode."""
        if content_type not in CONTENT_TYPES:
            return json.dumps({"ok": False, "error": f"content_type must be one of {CONTENT_TYPES}"})
        try:
            base = normalize_addon_base(addon_url)
            video_id = build_video_id(imdb_id, content_type, season, episode)
            streams = await fetch_streams(base, content_type, video_id)
        except HttpError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {
                "ok": True,
                "video_id": video_id,
                "addon": base,
                "count": min(len(streams), MAX_STREAMS),
                "total_available": len(streams),
                "streams": [_slim_stream(stream) for stream in streams[:MAX_STREAMS]],
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_find_streams(
        imdb_id: str,
        content_type: str = "movie",
        season: int = 0,
        episode: int = 0,
        per_addon: int = 10,
    ) -> str:
        """Ask every stream addon on the account for a title, all at once.

        This is what Stremio itself does when you open a title: it queries the
        installed addons in order and merges the results. Addons that fail or
        have nothing are reported separately rather than silently dropped.
        """
        if content_type not in CONTENT_TYPES:
            return json.dumps({"ok": False, "error": f"content_type must be one of {CONTENT_TYPES}"})
        from . import addon_collection

        try:
            video_id = build_video_id(imdb_id, content_type, season, episode)
            candidates, failures, providers_queried = await find_stream_candidates(
                imdb_id, content_type, video_id, per_addon
            )
        except (HttpError, api.ApiError, addon_collection.CollectionError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        if not providers_queried:
            return json.dumps(
                {
                    "ok": True,
                    "video_id": video_id,
                    "count": 0,
                    "streams": [],
                    "note": "no installed addon provides streams for this type; install one first",
                }
            )

        merged = [
            {"addon": candidate["addon"], **_slim_stream(candidate["stream"])}
            for candidate in candidates
        ]

        return json.dumps(
            {
                "ok": True,
                "video_id": video_id,
                "addons_queried": providers_queried,
                "count": len(merged),
                "streams": merged,
                "failed_addons": failures,
            },
            ensure_ascii=False,
        )
