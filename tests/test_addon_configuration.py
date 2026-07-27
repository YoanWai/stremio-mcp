import pytest

from stremio_mcp import addon_configuration


def entry(addon_id, name, url):
    return {
        "manifest": {
            "id": addon_id,
            "name": name,
            "behaviorHints": {"configurable": True, "configurationRequired": True},
        },
        "transportUrl": url,
    }


def test_configuration_url_preserves_a_manifest_path():
    assert (
        addon_configuration.configuration_url("https://addon.example/pick/manifest.json")
        == "https://addon.example/pick/configure"
    )


def test_resolve_directory_addon_prefers_an_exact_url():
    entries = [
        entry("one", "Shared Name", "https://one.example/manifest.json"),
        entry("two", "Shared Name", "https://two.example/manifest.json"),
    ]
    resolved = addon_configuration.resolve_directory_addon(
        entries, "https://two.example/manifest.json"
    )
    assert resolved["manifest"]["id"] == "two"


def test_configuration_page_parser_reports_fields_without_values():
    parser = addon_configuration.ConfigurationPageParser()
    parser.feed(
        """
        <title>Configure Test</title>
        <label for="token">API token</label>
        <input id="token" type="password" value="secret" required>
        <input id="language" type="text" placeholder="English">
        <input id="internal" type="hidden" value="private">
        """
    )
    result = parser.result()
    assert result["page_title"] == "Configure Test"
    assert result["fields"] == [
        {
            "key": "token",
            "type": "password",
            "placeholder": None,
            "required": True,
            "label": "API token",
        },
        {
            "key": "language",
            "type": "text",
            "placeholder": "English",
            "required": False,
            "label": None,
        },
    ]
    assert "secret" not in str(result)
    assert "private" not in str(result)


@pytest.mark.asyncio
async def test_complete_configuration_installs_the_matching_manifest(monkeypatch):
    directory_entry = entry(
        "community.test",
        "Test",
        "https://test.example/pick/manifest.json",
    )
    installed = []

    async def fetch_directory():
        return [directory_entry]

    async def get_manifest(url):
        assert url == "https://test.example/eng/manifest.json"
        return {
            "id": "community.test",
            "behaviorHints": {"configurable": True, "configurationRequired": False},
        }

    async def install(url, position, expected_addon_id):
        installed.append((url, position, expected_addon_id))
        return {"ok": True, "action": "installed"}

    monkeypatch.setattr(addon_configuration.addon_collection, "fetch_directory", fetch_directory)
    monkeypatch.setattr(addon_configuration, "get_json", get_manifest)
    monkeypatch.setattr(addon_configuration.addon_collection, "install", install)

    result = await addon_configuration.complete_configuration(
        "community.test",
        "https://test.example/eng/manifest.json",
        2,
    )

    assert result == {"stage": "installed", "ok": True, "action": "installed"}
    assert installed == [
        ("https://test.example/eng/manifest.json", 2, "community.test")
    ]
