
import pytest

from stremio_mcp import addon_collection as collection


def descriptor(addon_id, url=None, protected=False):
    return {
        "transportUrl": url or f"https://{addon_id}.example/manifest.json",
        "transportName": "http",
        "manifest": {"id": addon_id, "name": addon_id, "version": "1.0.0"},
        "flags": {"official": protected, "protected": protected},
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://x.example", "https://x.example/manifest.json"),
        ("https://x.example/", "https://x.example/manifest.json"),
        ("https://x.example/manifest.json", "https://x.example/manifest.json"),
        ("  https://x.example/manifest.json  ", "https://x.example/manifest.json"),
        ("stremio://x.example/manifest.json", "https://x.example/manifest.json"),
        ("https://x.example/stremio/v1", "https://x.example/stremio/v1"),
    ],
)
def test_normalize_manifest_url(raw, expected):
    assert collection.normalize_manifest_url(raw) == expected


def test_normalize_manifest_url_rejects_non_http():
    with pytest.raises(collection.CollectionError):
        collection.normalize_manifest_url("ftp://x.example")


def test_is_valid_descriptor_rejects_the_shape_that_wiped_the_collection():
    assert not collection.is_valid_descriptor({"transportUrl": "", "manifest": None})
    assert not collection.is_valid_descriptor({"manifestUrl": "http://a/manifest.json"})
    assert not collection.is_valid_descriptor({"transportUrl": "http://a", "manifest": {}})
    assert collection.is_valid_descriptor(descriptor("com.example"))


@pytest.mark.asyncio
async def test_push_refuses_to_drop_protected_addons(monkeypatch):
    previous = [descriptor("com.linvo.cinemeta", protected=True), descriptor("com.other")]
    monkeypatch.setattr(collection, "_snapshot", lambda addons: None)
    with pytest.raises(collection.CollectionError, match="protected"):
        await collection._push([descriptor("com.other")], previous)


@pytest.mark.asyncio
async def test_push_refuses_an_empty_collection(monkeypatch):
    monkeypatch.setattr(collection, "_snapshot", lambda addons: None)
    with pytest.raises(collection.CollectionError, match="empty"):
        await collection._push([], [descriptor("com.other")])


@pytest.mark.asyncio
async def test_push_sends_the_full_merged_list(monkeypatch):
    sent = {}

    async def fake_authed(method, payload=None):
        sent["method"] = method
        sent["payload"] = payload
        return {"result": {"success": True}}

    monkeypatch.setattr(collection.api, "authed", fake_authed)
    monkeypatch.setattr(collection, "_snapshot", lambda addons: "/tmp/backup.json")

    previous = [descriptor("com.linvo.cinemeta", protected=True)]
    updated = previous + [descriptor("com.new")]
    backup = await collection._push(updated, previous)

    assert backup == "/tmp/backup.json"
    assert sent["method"] == "addonCollectionSet"
    assert [entry["manifest"]["id"] for entry in sent["payload"]["addons"]] == [
        "com.linvo.cinemeta",
        "com.new",
    ]


@pytest.mark.asyncio
async def test_fetch_drops_broken_entries(monkeypatch):
    async def fake_authed(method, payload=None):
        return {
            "result": {
                "addons": [
                    {"transportUrl": "", "transportName": "", "manifest": None},
                    descriptor("com.good"),
                ]
            }
        }

    monkeypatch.setattr(collection.api, "authed", fake_authed)
    addons = await collection.fetch()
    assert [entry["manifest"]["id"] for entry in addons] == ["com.good"]
