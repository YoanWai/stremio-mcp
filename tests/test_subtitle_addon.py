import json
from dataclasses import replace
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlparse

import httpx
import pytest

from stremio_mcp import subtitle_addon, subtitle_sync


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    isolated = replace(subtitle_addon.settings, state_dir=tmp_path, addon_port=0)
    monkeypatch.setattr(subtitle_addon, "settings", isolated)
    monkeypatch.setattr(subtitle_sync, "settings", isolated)
    yield
    subtitle_addon.stop_server()


@pytest.fixture
def running_addon(monkeypatch):
    monkeypatch.setattr(subtitle_addon, "_subtitles", {})
    port = subtitle_addon.start_server()
    return f"http://127.0.0.1:{port}"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/subtitles/movie/tt1375666.json", ("movie", "tt1375666", {})),
        ("/subtitles/series/tt0944947%3A1%3A2.json", ("series", "tt0944947:1:2", {})),
        ("/subtitles/series/tt0944947:1:2.json", ("series", "tt0944947:1:2", {})),
        ("/subtitles/movie/tt1375666/videoHash=abc.json", ("movie", "tt1375666", {"videoHash": "abc"})),
        (
            "/subtitles/series/tt0944947%3A1%3A2/videoSize=42.json",
            ("series", "tt0944947:1:2", {"videoSize": "42"}),
        ),
        (
            "/subtitles/series/tt1%3A5%3A3/filename=The%20Show%20(2010)%20-%20S05E03.mkv&videoSize=42.json",
            ("series", "tt1:5:3", {"filename": "The Show (2010) - S05E03.mkv", "videoSize": "42"}),
        ),
    ],
)
def test_parse_subtitles_path(path, expected):
    assert subtitle_addon.parse_subtitles_path(path) == expected


@pytest.mark.parametrize(
    "path", ["/subtitles", "/subtitles/movie", "/manifest.json", "/other/a/b", "/subtitles/movie/tt1"]
)
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


def test_subtitle_list_offers_the_synced_subtitle_first(running_addon, tmp_path, monkeypatch):
    synced = subtitle_sync.synced_path("0123456789abcdef")
    synced.parent.mkdir(parents=True)
    synced.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nsynced\n")
    calls = []
    monkeypatch.setattr(
        subtitle_sync,
        "synced_subtitle",
        lambda content_type, video_id, extra: calls.append((content_type, video_id, extra)) or synced,
    )

    listing = httpx.get(
        f"{running_addon}/subtitles/series/tt1%3A5%3A3/filename=Show.S05E03-GRP.mkv.json"
    ).json()["subtitles"]

    assert calls == [("series", "tt1:5:3", {"filename": "Show.S05E03-GRP.mkv"})]
    assert listing[0]["lang"] == "heb"
    served = httpx.get(f"{running_addon}{urlparse(listing[0]['url']).path}")
    assert served.content == synced.read_bytes()


def test_a_failed_sync_still_lists_sideloaded_subtitles(running_addon, monkeypatch):
    def broken(content_type, video_id, extra):
        raise OSError("alass-cli not found")

    monkeypatch.setattr(subtitle_sync, "synced_subtitle", broken)
    monkeypatch.setattr(subtitle_addon, "_subtitles", {"tt1:5:3": [{"id": "abc", "lang": "heb"}]})

    listing = httpx.get(f"{running_addon}/subtitles/series/tt1%3A5%3A3/filename=x.mkv.json").json()

    assert [entry["id"] for entry in listing["subtitles"]] == ["abc"]


def test_synced_route_rejects_keys_that_are_not_cache_keys(running_addon):
    assert httpx.get(f"{running_addon}/synced/..%2F..%2Fauth.srt").status_code == 404


def test_start_server_shares_a_running_addon_on_the_preferred_port(monkeypatch):
    other = ThreadingHTTPServer((subtitle_addon.ADDON_BIND_HOST, 0), subtitle_addon._Handler)
    Thread(target=other.serve_forever, daemon=True).start()
    port = other.server_address[1]
    monkeypatch.setattr(subtitle_addon, "settings", replace(subtitle_addon.settings, addon_port=port))
    try:
        assert subtitle_addon.start_server() == port
        assert subtitle_addon._server is None
    finally:
        other.shutdown()
        other.server_close()


def test_start_server_without_fallback_refuses_a_taken_port(monkeypatch):
    other = ThreadingHTTPServer((subtitle_addon.ADDON_BIND_HOST, 0), subtitle_addon._Handler)
    monkeypatch.setattr(
        subtitle_addon, "settings", replace(subtitle_addon.settings, addon_port=other.server_address[1])
    )
    try:
        with pytest.raises(OSError):
            subtitle_addon.start_server(fallback=False)
    finally:
        other.server_close()


def test_lookup_sees_subtitles_another_process_added(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitle_addon, "_subtitles", {})
    folder = subtitle_addon.settings.subtitle_dir
    folder.mkdir(parents=True)
    (folder / "abc.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n")
    (folder / "index.json").write_text(json.dumps({"tt9": [{"id": "abc", "lang": "heb"}]}))

    assert subtitle_addon.lookup("tt9")[0]["id"] == "abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "installed_url,version,expect_install",
    [
        ("http://10.0.0.5:9876/manifest.json", None, False),
        ("http://10.0.0.9:51392/manifest.json", None, True),
        ("http://10.0.0.5:9876/manifest.json", "1.1.0", True),
        (None, None, True),
    ],
)
async def test_refresh_account_entry_points_the_account_at_this_server(
    monkeypatch, installed_url, version, expect_install
):
    monkeypatch.setattr(subtitle_addon, "_local_address", lambda: "10.0.0.5")
    monkeypatch.setattr(subtitle_addon, "_server_port", 9876)
    installed = []
    if installed_url:
        manifest = {**subtitle_addon._manifest(), **({"version": version} if version else {})}
        installed.append({"transportUrl": installed_url, "manifest": manifest})

    async def fake_fetch():
        return installed

    installs = []

    async def fake_install():
        installs.append(subtitle_addon.manifest_url())

    monkeypatch.setattr(subtitle_addon.addon_collection, "fetch", fake_fetch)
    monkeypatch.setattr(subtitle_addon, "install_to_account", fake_install)

    assert await subtitle_addon.refresh_account_entry() is expect_install
    assert installs == (["http://10.0.0.5:9876/manifest.json"] if expect_install else [])


@pytest.mark.asyncio
async def test_refresh_account_entry_never_publishes_a_loopback_address(monkeypatch):
    monkeypatch.setattr(subtitle_addon, "_local_address", lambda: "127.0.0.1")

    async def unexpected():
        pytest.fail("the account must not be touched while offline")

    monkeypatch.setattr(subtitle_addon.addon_collection, "fetch", unexpected)
    monkeypatch.setattr(subtitle_addon, "install_to_account", unexpected)

    assert await subtitle_addon.refresh_account_entry() is False
