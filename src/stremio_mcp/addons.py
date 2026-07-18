"""Addon query tools built on Stremio's public HTTP addon protocol.

Talks only to public addon endpoints (Cinemeta catalog/meta and any
user-supplied addon's manifest/stream routes). No Stremio account state.
"""

import json
from typing import Any
from urllib.parse import quote

import httpx

CINEMETA_BASE = "https://v3-cinemeta.strem.io"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_STREAMS = 30


async def _get_json(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    ) as client:
        try:
            response = await client.get(url)
        except httpx.HTTPError as error:
            raise RuntimeError(f"Request to {url} failed: {error}") from error
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} from {url}")
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"Invalid JSON from {url}: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected response shape from {url}: expected object")
    return payload


def _normalize_addon_base(addon_url: str) -> str:
    base = addon_url.strip().rstrip("/")
    if base.endswith("/manifest.json"):
        base = base[: -len("/manifest.json")]
    if not base.startswith(("http://", "https://")):
        raise RuntimeError(f"addon_url must be an http(s) URL, got {addon_url!r}")
    return base


def _slim_meta_entry(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "year": meta.get("year") or meta.get("releaseInfo"),
        "imdbRating": meta.get("imdbRating"),
        "genres": meta.get("genres") or meta.get("genre"),
    }


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_search(query: str, content_type: str = "movie", limit: int = 10) -> str:
        """Search movies or series by name via the public Cinemeta catalog."""
        if content_type not in ("movie", "series"):
            return f"Error: content_type must be 'movie' or 'series', got {content_type!r}"
        url = f"{CINEMETA_BASE}/catalog/{content_type}/top/search={quote(query)}.json"
        try:
            payload = await _get_json(url)
        except RuntimeError as error:
            return f"Error searching Cinemeta: {error}"
        metas = payload.get("metas")
        if not isinstance(metas, list):
            return f"Error: Cinemeta response has no 'metas' list for {url}"
        results = [_slim_meta_entry(meta) for meta in metas[:limit]]
        return json.dumps(
            {
                "query": query,
                "content_type": content_type,
                "count": len(results),
                "results": results,
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_get_meta(imdb_id: str, content_type: str = "movie") -> str:
        """Get full metadata for a title by IMDb id, including episode list for series."""
        if content_type not in ("movie", "series"):
            return f"Error: content_type must be 'movie' or 'series', got {content_type!r}"
        url = f"{CINEMETA_BASE}/meta/{content_type}/{quote(imdb_id)}.json"
        try:
            payload = await _get_json(url)
        except RuntimeError as error:
            return f"Error fetching meta: {error}"
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            return f"Error: Cinemeta response has no 'meta' object for {url}"
        trimmed: dict[str, Any] = {
            "id": meta.get("id"),
            "type": meta.get("type"),
            "name": meta.get("name"),
            "year": meta.get("year") or meta.get("releaseInfo"),
            "imdbRating": meta.get("imdbRating"),
            "genres": meta.get("genres") or meta.get("genre"),
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
        return json.dumps(trimmed, ensure_ascii=False)

    @mcp.tool()
    async def stremio_browse_catalog(
        content_type: str = "movie", genre: str = "", skip: int = 0
    ) -> str:
        """Browse the public Cinemeta top catalog, optionally filtered by genre."""
        if content_type not in ("movie", "series"):
            return f"Error: content_type must be 'movie' or 'series', got {content_type!r}"
        extras: list[str] = []
        if genre:
            extras.append(f"genre={quote(genre)}")
        if skip:
            extras.append(f"skip={skip}")
        extra_path = f"/{'&'.join(extras)}" if extras else ""
        url = f"{CINEMETA_BASE}/catalog/{content_type}/top{extra_path}.json"
        try:
            payload = await _get_json(url)
        except RuntimeError as error:
            return f"Error browsing catalog: {error}"
        metas = payload.get("metas")
        if not isinstance(metas, list):
            return f"Error: Cinemeta response has no 'metas' list for {url}"
        results = [_slim_meta_entry(meta) for meta in metas]
        return json.dumps(
            {
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
        """Fetch a Stremio addon's manifest and summarize its capabilities."""
        try:
            base = _normalize_addon_base(addon_url)
            manifest = await _get_json(f"{base}/manifest.json")
        except RuntimeError as error:
            return f"Error fetching addon manifest: {error}"
        return json.dumps(
            {
                "addon_url": base,
                "id": manifest.get("id"),
                "name": manifest.get("name"),
                "version": manifest.get("version"),
                "description": manifest.get("description"),
                "resources": manifest.get("resources"),
                "types": manifest.get("types"),
                "idPrefixes": manifest.get("idPrefixes"),
                "catalogs": [
                    {"type": catalog.get("type"), "id": catalog.get("id"), "name": catalog.get("name")}
                    for catalog in manifest.get("catalogs", [])
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
        """Query a Stremio addon for streams for a movie or a series episode."""
        if content_type not in ("movie", "series"):
            return f"Error: content_type must be 'movie' or 'series', got {content_type!r}"
        video_id = imdb_id
        if content_type == "series":
            if not (season and episode):
                return "Error: season and episode are required for series streams"
            video_id = f"{imdb_id}:{season}:{episode}"
        try:
            base = _normalize_addon_base(addon_url)
            payload = await _get_json(f"{base}/stream/{content_type}/{quote(video_id)}.json")
        except RuntimeError as error:
            return f"Error fetching streams: {error}"
        streams = payload.get("streams")
        if not isinstance(streams, list):
            return f"Error: addon response has no 'streams' list for {base}"
        slim_streams = [
            {
                "name": stream.get("name"),
                "title": stream.get("title"),
                "description": stream.get("description"),
                "url": stream.get("url"),
                "infoHash": stream.get("infoHash"),
                "fileIdx": stream.get("fileIdx"),
                "behaviorHints": stream.get("behaviorHints"),
            }
            for stream in streams[:MAX_STREAMS]
            if isinstance(stream, dict)
        ]
        return json.dumps(
            {
                "video_id": video_id,
                "addon": base,
                "count": len(slim_streams),
                "total_available": len(streams),
                "streams": slim_streams,
            },
            ensure_ascii=False,
        )
