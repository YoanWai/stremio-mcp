import pytest

from stremio_mcp import tv

MEDIA_SESSION_DUMP = """
  Sessions Stack - have 2 sessions
    com.stremio.one/MediaSessionCompat (userId=0)
      package=com.stremio.one
      state=PlaybackState {state=PLAYING(3), position=61000, buffered position=0, \
speed=1.0, updated=9000000, actions=1}
      metadata: size=3, description=Inception, null, null
"""


def test_parse_playback_reads_a_live_session():
    status = tv._parse_playback(MEDIA_SESSION_DUMP)
    assert status["app"] == "Stremio"
    assert status["state"] == "playing"
    assert status["title"] == "Inception"
    assert status["position"] == 61000


def test_parse_playback_with_no_stremio_session():
    status = tv._parse_playback("Sessions Stack - have 0 sessions")
    assert status["app"] is None
    assert status["state"] == "stopped"


def test_video_id_requires_season_and_episode_for_series():
    assert tv._video_id("tt1", "movie", None, None) == "tt1"
    assert tv._video_id("tt1", "series", 2, 3) == "tt1:2:3"
    with pytest.raises(tv.TVError):
        tv._video_id("tt1", "series", None, None)


def test_clear_default_player_does_not_wipe_app_data():
    """pm clear would destroy the TV's Stremio login and library cache."""
    import inspect

    source = inspect.getsource(tv.AndroidTV.clear_default_player)
    assert "pm clear" not in source
    assert "clear-defaults" in source


def test_android_tv_requires_a_host():
    with pytest.raises(tv.TVError):
        tv.AndroidTV("", 5555, "adb")
