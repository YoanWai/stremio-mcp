import base64
import json
import zlib
from urllib.parse import unquote, urlsplit

import pytest

from stremio_mcp import desktop_play


def test_encode_stream_matches_stremio_core_vector():
    stream = {"url": "http://domain.root/some/path"}
    assert (
        desktop_play.encode_stream(stream)
        == "eAEBJgDZ/3sidXJsIjoiaHR0cDovL2RvbWFpbi5yb290L3NvbWUvcGF0aCJ9AYANjw=="
    )


def test_player_uri_contains_the_full_resume_context():
    stream = {"infoHash": "a" * 40, "fileIdx": 2}
    uri = desktop_play.player_uri(
        stream,
        "https://streams.example/config/manifest.json",
        "series",
        "tt123",
        "tt123:2:4",
    )
    segments = [unquote(segment) for segment in urlsplit(uri).path.split("/") if segment]
    decoded = zlib.decompress(base64.b64decode(segments[1]))
    assert json.loads(decoded) == stream
    assert segments[2:] == [
        "https://streams.example/config/manifest.json",
        "https://v3-cinemeta.strem.io/manifest.json",
        "series",
        "tt123",
        "tt123:2:4",
    ]


def test_torrent_playback_stream_uses_the_local_server_route():
    stream = {
        "name": "Torrent",
        "infoHash": "A" * 40,
        "fileIdx": 2,
        "sources": ["tracker:https://tracker.example/announce"],
        "fileMustInclude": ["video"],
    }
    converted = desktop_play.torrent_playback_stream(stream)
    assert converted == {
        "name": "Torrent",
        "url": (
            "http://127.0.0.1:11470/"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/2"
            "?tr=tracker%3Ahttps%3A%2F%2Ftracker.example%2Fannounce&f=video"
        ),
    }


@pytest.mark.asyncio
async def test_play_selects_from_merged_stream_order(monkeypatch, tmp_path):
    candidates = [
        {
            "addon": "First",
            "addon_id": "first",
            "transport_url": "https://first.example/manifest.json",
            "stream": {"url": "https://video.example/first.mp4"},
        },
        {
            "addon": "Second",
            "addon_id": "second",
            "transport_url": "https://second.example/manifest.json",
            "stream": {"infoHash": "b" * 40, "fileIdx": 1, "title": "1080p"},
        },
    ]
    opened = []

    async def find_stream_candidates(*args):
        return candidates, [], 2

    async def wait_for_server(wait_seconds):
        assert wait_seconds == 9

    async def open_deep_link(uri):
        opened.append(uri)

    monkeypatch.setattr(desktop_play.desktop, "app_path", lambda: tmp_path)
    monkeypatch.setattr(desktop_play.addons, "find_stream_candidates", find_stream_candidates)
    monkeypatch.setattr(desktop_play, "wait_for_server", wait_for_server)
    monkeypatch.setattr(desktop_play.desktop, "_open_deep_link", open_deep_link)

    result = await desktop_play.play("tt123", "series", 2, 4, None, 1, 9)

    assert result["ok"] is True
    assert result["video_id"] == "tt123:2:4"
    assert result["addon_id"] == "second"
    assert result["stream"]["source"] == "torrent"
    assert result["resume_from_library"] is True
    assert len(opened) == 1
    assert "tt123%3A2%3A4" in opened[0]
    encoded_stream = unquote(urlsplit(opened[0]).path.split("/")[2])
    decoded_stream = json.loads(zlib.decompress(base64.b64decode(encoded_stream)))
    assert decoded_stream["url"].startswith(
        "http://127.0.0.1:11470/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "stream_index", "message"),
    [
        ("episode", 0, "content_type must be one of"),
        ("movie", -1, "stream_index must be zero or greater"),
    ],
)
async def test_play_rejects_invalid_selection_arguments(
    monkeypatch, tmp_path, content_type, stream_index, message
):
    monkeypatch.setattr(desktop_play.desktop, "app_path", lambda: tmp_path)

    with pytest.raises(desktop_play.HttpError, match=message):
        await desktop_play.play("tt123", content_type, 0, 0, None, stream_index, 1)


@pytest.mark.asyncio
async def test_play_reports_when_addons_return_no_streams(monkeypatch, tmp_path):
    async def find_stream_candidates(*args):
        return [], [{"addon": "Broken", "error": "HTTP 500"}], 1

    monkeypatch.setattr(desktop_play.desktop, "app_path", lambda: tmp_path)
    monkeypatch.setattr(desktop_play.addons, "find_stream_candidates", find_stream_candidates)

    with pytest.raises(
        desktop_play.HttpError,
        match="no installed addon returned a stream.*Broken",
    ):
        await desktop_play.play("tt123", "movie", 0, 0, None, 0, 1)


@pytest.mark.asyncio
async def test_play_rejects_out_of_range_stream_index(monkeypatch, tmp_path):
    async def find_stream_candidates(*args):
        return [
            {
                "addon": "First",
                "addon_id": "first",
                "transport_url": "https://first.example/manifest.json",
                "stream": {"url": "https://video.example/movie.mp4"},
            }
        ], [], 1

    monkeypatch.setattr(desktop_play.desktop, "app_path", lambda: tmp_path)
    monkeypatch.setattr(desktop_play.addons, "find_stream_candidates", find_stream_candidates)

    with pytest.raises(desktop_play.HttpError, match="outside the 1 available streams"):
        await desktop_play.play("tt123", "movie", 0, 0, None, 1, 1)


@pytest.mark.asyncio
async def test_play_rejects_invalid_torrent_stream(monkeypatch, tmp_path):
    async def find_stream_candidates(*args):
        return [
            {
                "addon": "Torrent",
                "addon_id": "torrent",
                "transport_url": "https://torrent.example/manifest.json",
                "stream": {"infoHash": "invalid"},
            }
        ], [], 1

    async def wait_for_server(wait_seconds):
        assert wait_seconds == 1

    monkeypatch.setattr(desktop_play.desktop, "app_path", lambda: tmp_path)
    monkeypatch.setattr(desktop_play.addons, "find_stream_candidates", find_stream_candidates)
    monkeypatch.setattr(desktop_play, "wait_for_server", wait_for_server)

    with pytest.raises(desktop_play.HttpError, match="invalid infoHash"):
        await desktop_play.play("tt123", "movie", 0, 0, None, 0, 1)
