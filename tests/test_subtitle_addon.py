import pytest

from stremio_mcp import subtitle_addon


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/subtitles/movie/tt1375666.json", "tt1375666"),
        ("/subtitles/series/tt0944947%3A1%3A2.json", "tt0944947:1:2"),
        ("/subtitles/series/tt0944947:1:2.json", "tt0944947:1:2"),
        ("/subtitles/movie/tt1375666/videoHash=abc.json", "tt1375666"),
        ("/subtitles/series/tt0944947%3A1%3A2/videoSize=42.json", "tt0944947:1:2"),
    ],
)
def test_parse_subtitles_path(path, expected):
    assert subtitle_addon.parse_subtitles_path(path) == expected


@pytest.mark.parametrize("path", ["/subtitles", "/subtitles/movie", "/manifest.json", "/other/a/b"])
def test_parse_subtitles_path_rejects_other_routes(path):
    assert subtitle_addon.parse_subtitles_path(path) is None


def test_video_id():
    assert subtitle_addon.video_id("tt1375666") == "tt1375666"
    assert subtitle_addon.video_id("tt0944947", 1, 2) == "tt0944947:1:2"
    assert subtitle_addon.video_id("tt0944947", 1, 0) == "tt0944947"


def test_lookup_falls_back_to_the_bare_title(monkeypatch):
    monkeypatch.setattr(
        subtitle_addon, "_subtitles", {"tt1": [{"id": "a", "lang": "heb"}]}, raising=False
    )
    assert subtitle_addon.lookup("tt1:3:4")[0]["id"] == "a"
    assert subtitle_addon.lookup("tt2") == []


def test_lookup_prefers_the_exact_episode(monkeypatch):
    monkeypatch.setattr(
        subtitle_addon,
        "_subtitles",
        {"tt1": [{"id": "title"}], "tt1:3:4": [{"id": "episode"}]},
        raising=False,
    )
    assert subtitle_addon.lookup("tt1:3:4")[0]["id"] == "episode"


def test_manifest_declares_the_subtitles_resource():
    manifest = subtitle_addon._manifest()
    assert manifest["resources"] == ["subtitles"]
    assert manifest["id"] == subtitle_addon.ADDON_ID
    assert set(manifest["types"]) == {"movie", "series"}
