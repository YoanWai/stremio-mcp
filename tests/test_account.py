import pytest

from stremio_mcp import account, api


def item(**state):
    return {"_id": "tt1", "name": "T", "type": "movie", "state": {**account._new_state(), **state}}


def test_is_watched_uses_the_flag():
    assert account.is_watched(item(flaggedWatched=1))
    assert not account.is_watched(item(flaggedWatched=0))


def test_is_watched_uses_the_duration_threshold():
    assert account.is_watched(item(duration=1000, timeWatched=800))
    assert not account.is_watched(item(duration=1000, timeWatched=600))
    assert not account.is_watched(item(duration=0, timeWatched=99999))


def test_summarize_reports_progress():
    summary = account._summarize(item(duration=1000, timeOffset=250))
    assert summary["position_ms"] == 250
    assert summary["progress"] == 0.25


def test_summarize_handles_a_missing_duration():
    assert account._summarize(item())["progress"] is None


def test_library_item_has_every_field_the_api_stores():
    built = account._library_item("tt1", "movie", {"name": "X", "year": 2020})
    assert built["_id"] == "tt1"
    assert built["removed"] is False
    assert built["year"] == "2020"
    assert set(built["state"]) == set(account._new_state())


def test_current_key_without_any_source(monkeypatch, tmp_path):
    import dataclasses

    monkeypatch.setattr(
        api, "settings", dataclasses.replace(api.settings, stremio_auth_key="", state_dir=tmp_path)
    )
    monkeypatch.setattr(api, "_session_auth_key", None, raising=False)
    monkeypatch.setattr(api, "_cache_loaded", False, raising=False)
    monkeypatch.setattr(api, "_load_cached_key", lambda: None)
    with pytest.raises(api.ApiError, match="No Stremio auth key"):
        api.current_key()


def test_api_call_sends_the_type_field_stremio_core_sends():
    assert "addonCollectionSet"[0].upper() + "addonCollectionSet"[1:] == "AddonCollectionSet"


@pytest.mark.asyncio
async def test_get_items_reports_an_invalid_api_shape(monkeypatch):
    async def fake_authed(method, payload):
        return {"result": {}}

    monkeypatch.setattr(account.api, "authed", fake_authed)
    with pytest.raises(api.ApiError, match="no library item list"):
        await account.get_items()
