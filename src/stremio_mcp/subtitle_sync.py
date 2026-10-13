"""Hebrew subtitles re-timed to the exact release Stremio is playing.

Releases of one episode differ by recaps and ad-break cuts, so a subtitle timed
for another release drifts in steps. Stremio tells subtitle addons the playing
file's name, which picks the closest Hebrew subtitle and, when an English one
matches the release better, aligns the Hebrew to it with alass.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from . import addon_collection, addons, api
from .config import settings
from .net import USER_AGENT

TARGET_LANGUAGE = "heb"
REFERENCE_LANGUAGE = "eng"
SOURCES_TTL_SECONDS = 600
DOWNLOAD_ATTEMPTS = 3

_SOURCE_MARKERS = (
    ("hdtv", {"hdtv", "pdtv", "sdtv"}),
    ("web", {"web", "webrip", "webdl"}),
    ("bluray", {"bluray", "bdrip", "brrip", "remux"}),
)
_FILE_EXTENSION = re.compile(r"\.(mkv|mp4|avi|srt)$", re.IGNORECASE)
_RELEASE_GROUP = re.compile(r"-([A-Za-z0-9]+)$")
_CACHE_KEY = re.compile(r"[0-9a-f]{16}")

_sources_cache: tuple[float, list[dict[str, Any]]] | None = None
_sources_lock = threading.Lock()


@dataclass(frozen=True)
class Candidate:
    lang: str
    url: str
    release: str


def release_traits(name: str) -> tuple[str, str]:
    """The release group and source (hdtv, web or bluray) a release name carries."""
    stem = _FILE_EXTENSION.sub("", name.strip())
    group_match = _RELEASE_GROUP.search(stem)
    group = group_match[1].lower() if group_match else ""
    tokens = set(re.split(r"[^a-z0-9]+", stem.lower()))
    for source, markers in _SOURCE_MARKERS:
        if tokens & markers:
            return group, source
    return group, ""


def match_score(release: str, playing: str) -> int:
    group, source = release_traits(release)
    playing_group, playing_source = release_traits(playing)
    score = 0
    if group and group == playing_group:
        score += 4
    if source and playing_source:
        if source == playing_source:
            score += 2
        # Broadcast cuts carry recaps and ad breaks that web and disc releases lack.
        if (source == "hdtv") == (playing_source == "hdtv"):
            score += 1
    return score


def rank(candidates: list[Candidate], playing: str) -> list[Candidate]:
    return sorted(candidates, key=lambda candidate: -match_score(candidate.release, playing))


def drop_cues_before_start(srt_text: str) -> str:
    """Remove cues alass pushed before 0:00, which are lines this release cut."""
    kept = []
    for block in re.split(r"\n\s*\n", srt_text.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start, end = (part.strip() for part in lines[timing_index].split("-->", 1))
        if "-" in end:
            continue
        if "-" in start:
            start = "00:00:00,000"
        kept.append("\n".join([f"{start} --> {end}", *lines[timing_index + 1 :]]))
    return "".join(f"{number}\n{cue}\n\n" for number, cue in enumerate(kept, 1))


def align(reference: bytes, subtitle: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as workdir:
        folder = Path(workdir)
        (folder / "reference.srt").write_bytes(reference)
        (folder / "subtitle.srt").write_bytes(subtitle)
        subprocess.run(
            [
                "alass-cli",
                "--allow-negative-timestamps",
                str(folder / "reference.srt"),
                str(folder / "subtitle.srt"),
                str(folder / "aligned.srt"),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        aligned = (folder / "aligned.srt").read_text(encoding="utf-8")
    return drop_cues_before_start(aligned).encode("utf-8")


def synced_path(requested_key: str) -> Path | None:
    """The cached file for a key taken from a URL, or None for anything else."""
    if not _CACHE_KEY.fullmatch(requested_key):
        return None
    return _cache_path(requested_key)


def _cache_path(cache_key: str) -> Path:
    return settings.state_dir / "synced" / f"{cache_key}.srt"


def synced_subtitle(content_type: str, video_id: str, extra: dict[str, str]) -> Path | None:
    """The best Hebrew subtitle for the playing file, aligned when that helps."""
    playing = extra.get("filename")
    if not playing:
        return None
    cache_key = hashlib.sha256(f"{video_id}|{playing}".encode()).hexdigest()[:16]
    cached = _cache_path(cache_key)
    if cached.is_file():
        return cached

    with _http_client() as http:
        candidates = _candidates(http, content_type, video_id, extra)
        target = _first_valid(
            http, rank([c for c in candidates if c.lang == TARGET_LANGUAGE], playing)
        )
        if target is None:
            _log(f"no usable {TARGET_LANGUAGE} subtitle for {video_id} {playing}")
            return None
        target_candidate, content = target
        target_score = match_score(target_candidate.release, playing)
        better_references = [
            candidate
            for candidate in rank([c for c in candidates if c.lang == REFERENCE_LANGUAGE], playing)
            if match_score(candidate.release, playing) > target_score
        ]
        reference = _first_valid(http, better_references)

    if reference is not None:
        content = align(reference[1], content)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(content)
    _log(
        f"{video_id} {playing}: {target_candidate.release or target_candidate.url}"
        + (f" aligned to {reference[0].release}" if reference else " as is")
    )
    return cached


def _candidates(
    http: httpx.Client, content_type: str, video_id: str, extra: dict[str, str]
) -> list[Candidate]:
    encoded_extra = "&".join(
        f"{quote(name, safe='')}={quote(value, safe='')}" for name, value in extra.items()
    )
    found = []
    for base in _subtitle_sources(content_type, video_id):
        url = f"{base}/subtitles/{content_type}/{quote(video_id, safe='')}/{encoded_extra}.json"
        try:
            response = http.get(url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            _log(f"subtitle source failed: {url}: {error}")
            continue
        listing = payload.get("subtitles") if isinstance(payload, dict) else None
        if not isinstance(listing, list):
            _log(f"subtitle source returned no subtitle list: {url}")
            continue
        found.extend(
            Candidate(entry["lang"], entry["url"], entry.get("movieReleaseName") or entry.get("id") or "")
            for entry in listing
            if entry.get("lang") in (TARGET_LANGUAGE, REFERENCE_LANGUAGE) and entry.get("url")
        )
    return found


def _first_valid(http: httpx.Client, ranked: list[Candidate]) -> tuple[Candidate, bytes] | None:
    for candidate in ranked[:DOWNLOAD_ATTEMPTS]:
        try:
            response = http.get(candidate.url)
            response.raise_for_status()
        except httpx.HTTPError as error:
            _log(f"subtitle download failed: {candidate.url}: {error}")
            continue
        # Some sources answer 200 with an error page instead of the file.
        if b"-->" in response.content:
            return candidate, response.content
        _log(f"not a subtitle file: {candidate.url}")
    return None


def _subtitle_sources(content_type: str, video_id: str) -> list[str]:
    """Base URLs of the account's other subtitle addons that cover this video."""
    from .subtitle_addon import ADDON_ID

    imdb_id = video_id.split(":", 1)[0]
    return [
        addons.normalize_addon_base(entry["transportUrl"])
        for entry in _installed_addons()
        if addon_collection.descriptor_id(entry) != ADDON_ID
        and entry["transportUrl"].endswith("/manifest.json")
        and addons.supports_resource(entry["manifest"], "subtitles", content_type, imdb_id)
    ]


def _installed_addons() -> list[dict[str, Any]]:
    global _sources_cache
    with _sources_lock:
        if _sources_cache and time.monotonic() - _sources_cache[0] < SOURCES_TTL_SECONDS:
            return _sources_cache[1]
        with _http_client() as http:
            response = http.post(
                f"{api.API_BASE}addonCollectionGet",
                json={"type": "AddonCollectionGet", "authKey": api.current_key(), "update": True},
            )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("addons"), list):
            raise api.ApiError(f"addonCollectionGet returned no addon list: {payload!r:.200}")
        installed = [entry for entry in result["addons"] if addon_collection.is_valid_descriptor(entry)]
        _sources_cache = (time.monotonic(), installed)
        return installed


def _http_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(10.0), follow_redirects=True
    )


def _log(message: str) -> None:
    print(f"[mcp-subtitles] {message}", file=sys.stderr, flush=True)
