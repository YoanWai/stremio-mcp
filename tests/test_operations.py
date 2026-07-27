import pytest

from stremio_mcp import operations


def descriptor(addon_id, version="1.0.0", url=None, protected=False):
    return {
        "transportUrl": url or f"https://{addon_id}.example/manifest.json",
        "transportName": "http",
        "manifest": {
            "id": addon_id,
            "name": addon_id,
            "version": version,
            "resources": [],
            "types": [],
            "catalogs": [],
        },
        "flags": {"official": protected, "protected": protected},
    }


def test_legacy_manifest_conversion_matches_stremio_core_mapping():
    manifest = operations._legacy_manifest(
        {
            "id": "legacy",
            "name": "Legacy",
            "version": "1.2.3",
            "description": "Example",
            "methods": ["meta.get", "meta.find", "stream.find", "subtitles.find"],
            "types": ["movie"],
            "idProperty": "imdb_id",
        }
    )

    assert manifest["resources"] == ["meta", "stream", "subtitles"]
    assert manifest["catalogs"] == [
        {"type": "movie", "id": "top", "name": None, "extra": []}
    ]
    assert manifest["idPrefixes"] == ["tt"]


@pytest.mark.asyncio
async def test_live_manifest_uses_legacy_json_rpc_route(monkeypatch):
    requested = []

    async def fake_get_json(url):
        requested.append(url)
        return {
            "result": {
                "manifest": {
                    "id": "legacy",
                    "name": "Legacy",
                    "version": "1.0.0",
                    "methods": ["subtitles.find"],
                    "types": ["movie"],
                }
            }
        }

    monkeypatch.setattr(operations, "get_json", fake_get_json)
    manifest = await operations.live_manifest(
        descriptor("legacy", url="https://legacy.example/stremio/v1")
    )

    assert requested[0].endswith(f"/q.json?b={operations._LEGACY_MANIFEST_PARAM}")
    assert manifest["resources"] == ["subtitles"]


def test_semver_precedence_handles_prereleases_and_build_metadata():
    assert operations._semver_key("1.0.0") > operations._semver_key("1.0.0-rc.1")
    assert operations._semver_key("1.0.0-rc.2") > operations._semver_key("1.0.0-rc.1")
    assert operations._semver_key("1.0.1+build.4") > operations._semver_key("1.0.0")


@pytest.mark.asyncio
async def test_addon_upgrades_applies_all_new_manifests_through_push(monkeypatch):
    current = [
        descriptor("protected", protected=True),
        descriptor("upgrade", version="1.0.0"),
    ]

    async def fake_fetch():
        return current

    async def fake_live(entry):
        manifest = {**entry["manifest"]}
        if manifest["id"] == "upgrade":
            manifest["version"] = "2.0.0"
        return manifest

    pushed = []

    async def fake_push(updated, previous):
        pushed.append((updated, previous))
        return "/tmp/backup.json"

    monkeypatch.setattr(operations.addon_collection, "fetch", fake_fetch)
    monkeypatch.setattr(operations, "live_manifest", fake_live)
    monkeypatch.setattr(operations.addon_collection, "_push", fake_push)

    result = await operations.addon_upgrades(apply=True)

    assert result["upgrade_count"] == 1
    assert result["applied"] == 1
    assert len(pushed) == 1
    assert pushed[0][0][0] == current[0]
    assert pushed[0][0][1]["manifest"]["version"] == "2.0.0"


@pytest.mark.asyncio
async def test_cast_devices_exposes_chromecast_and_dlna_only(monkeypatch):
    async def fake_casting_json(method, path, payload=None):
        return [
            {"id": "vlc", "name": "VLC", "type": "external"},
            {
                "id": "cast",
                "name": "TV",
                "type": "chromecast",
                "facility": "MDNS",
                "host": "192.0.2.1",
            },
            {
                "id": "dlna",
                "name": "Renderer",
                "type": "tv",
                "facility": "SSDP",
                "host": "192.0.2.2",
            },
        ]

    monkeypatch.setattr(operations, "_casting_json", fake_casting_json)
    devices = await operations.cast_devices()

    assert [device["type"] for device in devices] == ["chromecast", "dlna"]


@pytest.mark.asyncio
async def test_cast_play_resolves_name_and_posts_source(monkeypatch):
    async def fake_device(selector):
        assert selector == "TV"
        return {"id": "cast", "name": "TV", "type": "chromecast"}

    posted = []

    async def fake_player(device_id, payload):
        posted.append((device_id, payload))
        return {"state": 3, "source": payload["source"]}

    monkeypatch.setattr(operations, "_cast_device", fake_device)
    monkeypatch.setattr(operations, "_cast_player", fake_player)

    result = await operations.cast_play("TV", stream_url="https://video.example/movie.mp4")

    assert posted == [("cast", {"source": "https://video.example/movie.mp4"})]
    assert result["status"] == {"state": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_url", "info_hash"),
    [
        ("", ""),
        ("https://video.example/movie.mp4", "a" * 40),
    ],
)
async def test_cast_play_requires_exactly_one_source(
    monkeypatch, stream_url, info_hash
):
    async def fail_device(selector):
        raise AssertionError("device discovery must not run")

    monkeypatch.setattr(operations, "_cast_device", fail_device)

    with pytest.raises(operations.OperationsError, match="exactly one"):
        await operations.cast_play(
            "TV",
            stream_url=stream_url,
            info_hash=info_hash,
        )


@pytest.mark.asyncio
async def test_cast_play_rejects_negative_resume_before_device_request(monkeypatch):
    async def fail_device(selector):
        raise AssertionError("device discovery must not run")

    monkeypatch.setattr(operations, "_cast_device", fail_device)

    with pytest.raises(operations.OperationsError, match="cannot be negative"):
        await operations.cast_play(
            "TV",
            stream_url="https://video.example/movie.mp4",
            resume_position_ms=-1,
        )


@pytest.mark.asyncio
async def test_cast_control_validates_volume_before_request(monkeypatch):
    async def fake_device(selector):
        return {"id": "cast", "name": "TV", "type": "chromecast"}

    monkeypatch.setattr(operations, "_cast_device", fake_device)
    with pytest.raises(operations.OperationsError, match="between 0 and 1"):
        await operations.cast_control("TV", "volume", 2)
