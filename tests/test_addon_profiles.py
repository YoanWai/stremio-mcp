import json
from types import SimpleNamespace

import pytest

from stremio_mcp import addon_profiles


def descriptor(addon_id, protected=False):
    return {
        "transportUrl": f"https://{addon_id}.example/manifest.json",
        "manifest": {"id": addon_id, "name": addon_id},
        "flags": {"official": protected, "protected": protected},
    }


def test_profile_path_rejects_directory_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        addon_profiles,
        "settings",
        SimpleNamespace(state_dir=tmp_path),
    )
    with pytest.raises(addon_profiles.ProfileError):
        addon_profiles.profile_path("../anime")


def test_profile_files_use_the_snapshot_descriptor_format(monkeypatch, tmp_path):
    monkeypatch.setattr(
        addon_profiles,
        "settings",
        SimpleNamespace(state_dir=tmp_path),
    )
    addons = [descriptor("com.linvo.cinemeta", protected=True), descriptor("com.anime")]
    path = addon_profiles.profile_path("anime")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(addons))

    loaded = addon_profiles.read_profile(path)
    summary = addon_profiles.profile_summary(path, loaded)

    assert loaded == addons
    assert summary["name"] == "anime"
    assert summary["addon_ids"] == ["com.linvo.cinemeta", "com.anime"]


@pytest.mark.asyncio
async def test_apply_profile_uses_guarded_push(monkeypatch, tmp_path):
    monkeypatch.setattr(
        addon_profiles,
        "settings",
        SimpleNamespace(state_dir=tmp_path),
    )
    desired = [
        descriptor("com.linvo.cinemeta", protected=True),
        descriptor("com.minimal"),
    ]
    current = desired + [descriptor("com.extra")]
    path = addon_profiles.profile_path("minimal")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(desired))
    pushed = {}

    async def fetch():
        return current

    async def push(addons, previous):
        pushed["addons"] = addons
        pushed["previous"] = previous
        return "/tmp/backup.json"

    monkeypatch.setattr(addon_profiles.addon_collection, "fetch", fetch)
    monkeypatch.setattr(addon_profiles.addon_collection, "_push", push)

    result = await addon_profiles.apply_profile("minimal")

    assert result["ok"] is True
    assert result["before"] == 3
    assert result["after"] == 2
    assert result["backup"] == "/tmp/backup.json"
    assert pushed == {"addons": desired, "previous": current}
