import pytest

from stremio_mcp import addons
from stremio_mcp.net import HttpError


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://a.example/manifest.json", "https://a.example"),
        ("https://a.example/", "https://a.example"),
        ("https://a.example", "https://a.example"),
    ],
)
def test_normalize_addon_base(raw, expected):
    assert addons.normalize_addon_base(raw) == expected


def test_normalize_addon_base_rejects_non_http():
    with pytest.raises(HttpError):
        addons.normalize_addon_base("a.example")


def test_build_video_id():
    assert addons.build_video_id("tt1", "movie", 0, 0) == "tt1"
    assert addons.build_video_id("tt1", "series", 2, 5) == "tt1:2:5"


def test_build_video_id_requires_season_and_episode():
    with pytest.raises(HttpError):
        addons.build_video_id("tt1", "series", 0, 0)


@pytest.mark.parametrize(
    "manifest,expected",
    [
        ({"resources": ["stream"], "types": ["movie"], "idPrefixes": ["tt"]}, True),
        ({"resources": ["catalog", "meta"], "types": ["movie"]}, False),
        ({"resources": ["stream"], "types": ["series"]}, False),
        ({"resources": ["stream"], "types": ["movie"], "idPrefixes": ["kitsu"]}, False),
        ({"resources": [{"name": "stream"}], "types": ["movie"]}, True),
        ({"resources": ["stream"]}, True),
    ],
)
def test_supports_streams(manifest, expected):
    assert addons._supports_streams(manifest, "movie", "tt1375666") is expected
