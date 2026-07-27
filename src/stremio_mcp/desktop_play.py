"""Select an installed-addon stream and play it in Stremio desktop."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import zlib
from typing import Any
from urllib.parse import quote, urlencode

from . import addon_collection, addons, api, desktop
from .net import HttpError

CINEMETA_MANIFEST_URL = f"{addons.CINEMETA_BASE}/manifest.json"
_INFO_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def encode_stream(stream: dict[str, Any]) -> str:
    payload = json.dumps(stream, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(zlib.compress(payload, level=0)).decode()


def player_uri(
    stream: dict[str, Any],
    stream_transport_url: str,
    content_type: str,
    imdb_id: str,
    video_id: str,
) -> str:
    segments = (
        encode_stream(stream),
        stream_transport_url,
        CINEMETA_MANIFEST_URL,
        content_type,
        imdb_id,
        video_id,
    )
    return "stremio:///player/" + "/".join(quote(segment, safe="") for segment in segments)


def torrent_playback_stream(stream: dict[str, Any]) -> dict[str, Any]:
    info_hash = stream.get("infoHash")
    if not isinstance(info_hash, str) or not _INFO_HASH_RE.fullmatch(info_hash):
        raise HttpError("torrent stream returned an invalid infoHash")

    file_index = stream.get("fileIdx")
    if file_index is None:
        file_index = -1
    if not isinstance(file_index, int):
        raise HttpError("torrent stream returned a non-integer fileIdx")

    sources = stream.get("sources")
    if sources is None:
        sources = stream.get("announce", [])
    if not isinstance(sources, list) or not all(isinstance(source, str) for source in sources):
        raise HttpError("torrent stream returned an invalid sources list")

    file_filters = stream.get("fileMustInclude", [])
    if not isinstance(file_filters, list) or not all(
        isinstance(file_filter, str) for file_filter in file_filters
    ):
        raise HttpError("torrent stream returned an invalid fileMustInclude list")

    query = urlencode(
        [("tr", source) for source in sources] + [("f", file_filter) for file_filter in file_filters]
    )
    url = f"{desktop.STREAMING_SERVER_URL}/{info_hash.lower()}/{file_index}"
    if query:
        url = f"{url}?{query}"
    converted = {
        key: value
        for key, value in stream.items()
        if key not in {"infoHash", "fileIdx", "sources", "announce", "fileMustInclude"}
    }
    converted["url"] = url
    return converted


def stream_source(stream: dict[str, Any]) -> str:
    if stream.get("infoHash"):
        return "torrent"
    if stream.get("url"):
        return "url"
    if stream.get("ytId"):
        return "youtube"
    return "addon"


async def wait_for_server(wait_seconds: int) -> None:
    if await desktop.server_running():
        return
    await desktop._launch()
    for _ in range(max(0, wait_seconds)):
        if await desktop.server_running():
            return
        await asyncio.sleep(1)
    raise desktop.DesktopError(
        f"Stremio streaming server was not available within {max(0, wait_seconds)}s"
    )


async def play(
    imdb_id: str,
    content_type: str,
    season: int,
    episode: int,
    video_id: str,
    stream_index: int,
    wait_seconds: int,
) -> dict[str, Any]:
    if desktop.app_path() is None:
        raise desktop.DesktopError("Stremio desktop is not installed")
    if content_type not in addons.CONTENT_TYPES:
        raise HttpError(f"content_type must be one of {addons.CONTENT_TYPES}")
    if stream_index < 0:
        raise HttpError("stream_index must be zero or greater")
    resolved_video_id = video_id.strip() or addons.build_video_id(
        imdb_id, content_type, season, episode
    )
    candidates, failures, _ = await addons.find_stream_candidates(
        imdb_id, content_type, resolved_video_id
    )
    if not candidates:
        details = f"; failed addons: {failures}" if failures else ""
        raise HttpError(f"no installed addon returned a stream for {resolved_video_id}{details}")
    if stream_index >= len(candidates):
        raise HttpError(
            f"stream_index {stream_index} is outside the {len(candidates)} available streams"
        )

    selected = candidates[stream_index]
    stream = selected["stream"]
    await wait_for_server(wait_seconds)
    playback_stream = torrent_playback_stream(stream) if stream.get("infoHash") else stream
    await desktop._open_deep_link(
        player_uri(
            playback_stream,
            selected["transport_url"],
            content_type,
            imdb_id,
            resolved_video_id,
        )
    )
    return {
        "ok": True,
        "imdb_id": imdb_id,
        "video_id": resolved_video_id,
        "content_type": content_type,
        "stream_index": stream_index,
        "available_streams": len(candidates),
        "addon": selected["addon"],
        "addon_id": selected["addon_id"],
        "stream": {
            "name": stream.get("name"),
            "title": stream.get("title") or stream.get("description"),
            "source": stream_source(stream),
        },
        "resume_from_library": True,
        "failed_addons": failures,
    }


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_desktop_play(
        imdb_id: str,
        content_type: str = "movie",
        season: int = 0,
        episode: int = 0,
        video_id: str = "",
        stream_index: int = 0,
        wait_seconds: int = 20,
    ) -> str:
        """Select an installed-addon stream and start it in Stremio desktop.

        Streams follow installed addon order, then each addon's result order.
        stream_index selects from that merged list. For a continue-watching item,
        pass its video_id so Stremio restores the account's saved timeOffset.
        """
        try:
            result = await play(
                imdb_id,
                content_type,
                season,
                episode,
                video_id,
                stream_index,
                wait_seconds,
            )
        except (
            HttpError,
            api.ApiError,
            addon_collection.CollectionError,
            desktop.DesktopError,
        ) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(result, ensure_ascii=False)
