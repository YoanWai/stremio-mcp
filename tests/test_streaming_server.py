from pathlib import Path

import pytest

from stremio_mcp import streaming_server

HASH_A = "a" * 40
HASH_B = "b" * 40


def settings_for(path: Path):
    return {"values": {"cacheRoot": str(path), "cacheSize": 10_000}}


@pytest.mark.asyncio
async def test_active_streams_reports_safe_progress_fields(monkeypatch):
    engine = {
        HASH_A: {
            "name": "Example",
            "peers": 4,
            "downloaded": 25,
            "downloadSpeed": 8,
            "uploadSpeed": 2,
            "files": [{"name": "video.mp4", "length": 100}],
            "wires": [{"address": "192.0.2.1"}],
            "sources": {"tracker": ["192.0.2.2"]},
        }
    }

    async def fake_get_json(url):
        if url.endswith("/stats.json") and f"/{HASH_A}/" not in url:
            return engine
        return {"streamProgress": 0.25}

    monkeypatch.setattr(streaming_server, "get_json", fake_get_json)
    streams = await streaming_server.active_streams()

    assert streams == [
        {
            "info_hash": HASH_A,
            "name": "Example",
            "peers": 4,
            "downloaded_bytes": 25,
            "total_bytes": 100,
            "progress": 0.25,
            "download_speed_bps": 8,
            "upload_speed_bps": 2,
            "files": [
                {
                    "index": 0,
                    "name": "video.mp4",
                    "length_bytes": 100,
                    "progress": 0.25,
                }
            ],
        }
    ]
    assert "wires" not in streams[0]
    assert "sources" not in streams[0]


@pytest.mark.asyncio
async def test_cache_inventory_reads_configured_cache_root(tmp_path, monkeypatch):
    cache_root = tmp_path / "stremio-cache"
    entry = cache_root / HASH_A
    entry.mkdir(parents=True)
    (entry / "cache").write_bytes(b"1234")
    (entry / "bitfield").write_bytes(b"12")

    async def fake_settings():
        return settings_for(tmp_path)

    async def fake_stats():
        return {HASH_A: {}}

    monkeypatch.setattr(streaming_server.desktop, "server_settings", fake_settings)
    monkeypatch.setattr(streaming_server, "_active_stats", fake_stats)
    inventory = await streaming_server.cache_inventory()

    assert inventory["configured_limit_bytes"] == 10_000
    assert inventory["used_bytes"] == 6
    assert inventory["entry_count"] == 1
    assert inventory["entries"][0]["info_hash"] == HASH_A
    assert inventory["entries"][0]["file_count"] == 2
    assert inventory["entries"][0]["active"] is True


@pytest.mark.asyncio
async def test_purge_requires_explicit_confirmation():
    with pytest.raises(streaming_server.StreamingServerError, match="confirm=true"):
        await streaming_server.purge_cache(False)


@pytest.mark.asyncio
async def test_purge_stops_engine_and_deletes_only_selected_hash(tmp_path, monkeypatch):
    cache_root = tmp_path / "stremio-cache"
    for info_hash in (HASH_A, HASH_B):
        entry = cache_root / info_hash
        entry.mkdir(parents=True)
        (entry / "cache").write_bytes(info_hash.encode())

    async def fake_inventory():
        return {
            "cache_root": str(cache_root),
            "configured_limit_bytes": 10_000,
            "used_bytes": 80,
            "entry_count": 2,
            "entries": [
                {
                    "info_hash": HASH_A,
                    "size_bytes": 40,
                    "file_count": 1,
                    "modified_at": 1,
                    "active": True,
                },
                {
                    "info_hash": HASH_B,
                    "size_bytes": 40,
                    "file_count": 1,
                    "modified_at": 1,
                    "active": False,
                },
            ],
        }

    stopped = []

    async def fake_stop(info_hash):
        stopped.append(info_hash)

    monkeypatch.setattr(streaming_server, "cache_inventory", fake_inventory)
    monkeypatch.setattr(streaming_server, "_stop_engines", fake_stop)
    result = await streaming_server.purge_cache(True, HASH_A)

    assert stopped == [HASH_A]
    assert result["deleted_hashes"] == [HASH_A]
    assert not (cache_root / HASH_A).exists()
    assert (cache_root / HASH_B).is_dir()
