import base64
import time
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from stremio_mcp import content_features
from stremio_mcp.net import HttpError


def descriptor(addon_id, name, url, catalogs, types=None, prefixes=None):
    return {
        "transportUrl": url,
        "manifest": {
            "id": addon_id,
            "name": name,
            "version": "1.0.0",
            "resources": ["catalog"],
            "types": types or ["movie", "series"],
            "idPrefixes": prefixes,
            "catalogs": catalogs,
        },
        "flags": {"official": False, "protected": False},
    }


@pytest.mark.asyncio
async def test_trakt_sync_opens_account_authorization_when_unlinked(monkeypatch):
    async def fake_authed(method):
        assert method == "getUser"
        return {"result": {"_id": "user id", "trakt": {}}}

    opened = []
    monkeypatch.setattr(content_features.api, "authed", fake_authed)
    monkeypatch.setattr(
        content_features.webbrowser, "open", lambda url: opened.append(url) or True
    )

    result = await content_features.trakt_sync()

    assert result["linked"] is False
    assert result["authorization_opened"] is True
    assert opened == ["https://www.strem.io/trakt/auth/user%20id"]


@pytest.mark.asyncio
async def test_trakt_sync_installs_uid_manifest_when_linked(monkeypatch):
    async def fake_authed(method):
        return {
            "result": {
                "_id": "abc",
                "trakt": {
                    "created_at": int(time.time()),
                    "expires_in": 3600,
                    "access_token": "secret",
                },
            }
        }

    async def fake_get_json(url):
        assert url == "https://www.strem.io/trakt/addon/abc/manifest.json"
        return {"id": "trakt.addon", "version": "1.0.0"}

    installed = []

    async def fake_install(url, manifest):
        installed.append((url, manifest))
        return {"action": "installed"}

    monkeypatch.setattr(content_features.api, "authed", fake_authed)
    monkeypatch.setattr(content_features, "get_json", fake_get_json)
    monkeypatch.setattr(content_features, "_install_manifest", fake_install)

    result = await content_features.trakt_sync()

    assert result["linked"] is True
    assert result["action"] == "installed"
    assert installed[0][1]["id"] == "trakt.addon"


def test_calendar_requests_use_recent_series_and_manifest_limit():
    addon = descriptor(
        "calendar",
        "Calendar",
        "https://calendar.example/manifest.json",
        [
            {
                "type": "series",
                "id": "calendar-videos",
                "extra": [
                    {
                        "name": "calendarVideosIds",
                        "isRequired": True,
                        "optionsLimit": 2,
                    }
                ],
            }
        ],
        types=["series"],
        prefixes=["tt"],
    )
    library = [
        {"_id": "tt3", "type": "series", "_mtime": "3", "removed": False, "temp": False},
        {"_id": "tt1", "type": "series", "_mtime": "1", "removed": False, "temp": False},
        {"_id": "tt2", "type": "series", "_mtime": "2", "removed": False, "temp": False},
    ]

    requests = content_features._calendar_requests([addon], library)

    assert len(requests) == 1
    assert requests[0][2] == ["tt2", "tt3"]


@pytest.mark.asyncio
async def test_upcoming_episodes_reads_metas_detailed(monkeypatch):
    addon = descriptor(
        "calendar",
        "Calendar",
        "https://calendar.example/manifest.json",
        [
            {
                "type": "series",
                "id": "calendar-videos",
                "extra": [{"name": "calendarVideosIds", "optionsLimit": 100}],
            }
        ],
        types=["series"],
        prefixes=["tt"],
    )
    library = [
        {"_id": "tt1", "type": "series", "_mtime": "1", "removed": False, "temp": False}
    ]
    released = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

    async def fake_fetch():
        return [addon]

    async def fake_items():
        return library

    async def fake_get_json(url):
        assert "calendarVideosIds=tt1" in url
        return {
            "metasDetailed": [
                {
                    "id": "tt1",
                    "name": "Series",
                    "videos": [
                        {
                            "id": "tt1:1:2",
                            "season": 1,
                            "episode": 2,
                            "name": "Episode",
                            "released": released,
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(content_features.addon_collection, "fetch", fake_fetch)
    monkeypatch.setattr(content_features.account, "get_items", fake_items)
    monkeypatch.setattr(content_features, "get_json", fake_get_json)

    result = await content_features.upcoming_episodes()

    assert result["count"] == 1
    assert result["episodes"][0]["video_id"] == "tt1:1:2"


def test_watched_flags_decode_stremio_bitfield():
    encoded = base64.b64encode(zlib.compress(bytes([0b00000101]))).decode()
    serialized = f"tt1:1:3:3:{encoded}"

    flags = content_features._watched_flags(
        serialized, ["tt1:1:1", "tt1:1:2", "tt1:1:3", "tt1:1:4"]
    )

    assert flags == [True, False, True, False]


@pytest.mark.asyncio
async def test_next_unwatched_episode_skips_specials_and_resumes_current(monkeypatch):
    encoded = base64.b64encode(zlib.compress(bytes([0b00000001]))).decode()

    async def fake_items():
        return [
            {
                "_id": "tt1",
                "name": "Series",
                "state": {
                    "watched": f"tt1:1:1:1:{encoded}",
                    "video_id": "tt1:1:2",
                    "timeOffset": 2000,
                },
            }
        ]

    async def fake_get_json(url):
        return {
            "meta": {
                "id": "tt1",
                "name": "Series",
                "videos": [
                    {"id": "tt1:0:1", "season": 0, "episode": 1},
                    {"id": "tt1:1:2", "season": 1, "episode": 2},
                    {"id": "tt1:1:1", "season": 1, "episode": 1},
                ],
            }
        }

    monkeypatch.setattr(content_features.account, "get_items", fake_items)
    monkeypatch.setattr(content_features, "get_json", fake_get_json)

    result = await content_features.next_unwatched_episode("tt1")

    assert result["watched_episodes"] == 1
    assert result["next_episode"]["video_id"] == "tt1:1:2"
    assert result["next_episode"]["resume_position_ms"] == 2000


@pytest.mark.asyncio
async def test_catalog_search_reports_failed_addons(monkeypatch):
    catalog = {
        "type": "movie",
        "id": "top",
        "extra": [{"name": "search"}],
    }
    addons = [
        descriptor("good", "Good", "https://good.example/manifest.json", [catalog]),
        descriptor("bad", "Bad", "https://bad.example/manifest.json", [catalog]),
    ]

    async def fake_fetch():
        return addons

    async def fake_get_json(url):
        if "bad.example" in url:
            raise HttpError("HTTP 500")
        return {"metas": [{"id": "tt1", "name": "Result", "type": "movie"}]}

    monkeypatch.setattr(content_features.addon_collection, "fetch", fake_fetch)
    monkeypatch.setattr(content_features, "get_json", fake_get_json)

    result = await content_features.cross_catalog_search("result", "movie")

    assert result["catalogs_queried"] == 2
    assert result["count"] == 1
    assert result["failed_catalogs"] == [
        {"addon": "Bad", "catalog": "top", "error": "HTTP 500"}
    ]


@pytest.mark.asyncio
async def test_auto_fetch_subtitle_selects_language_and_feeds_add_subtitle(monkeypatch):
    requested = []

    async def fake_get_json(url):
        requested.append(url)
        return {
            "subtitles": [
                {"id": "1", "lang": "spa", "url": "https://subs.example/spa.srt"},
                {"id": "2", "lang": "eng", "url": "https://subs.example/eng.srt"},
            ]
        }

    async def fake_add(**kwargs):
        assert kwargs["subtitle_url"] == "https://subs.example/eng.srt"
        return {"video_id": "tt1", "subtitle": {"id": "local"}}

    monkeypatch.setattr(content_features, "get_json", fake_get_json)
    monkeypatch.setattr(content_features.subtitle_addon, "add_subtitle_url", fake_add)

    result = await content_features.auto_fetch_subtitle(
        "tt1",
        language="eng",
        video_hash="7ca79fe7ffed66c4",
        video_size=774475248,
        filename="movie.mp4",
    )

    assert "videoHash=7ca79fe7ffed66c4" in requested[0]
    assert "videoSize=774475248" in requested[0]
    assert result["provider_subtitle_id"] == "2"
