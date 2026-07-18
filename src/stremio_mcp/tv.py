"""Android TV control for Stremio over adb.

Drives a Stremio install on an Android TV (package ``com.stremio.one``) by
shelling out to ``adb``: opening titles via deep-link intents, sending remote
key events, and reading playback state from ``dumpsys``. The target device and
adb binary come from environment settings (see config).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from .config import settings

STREMIO_PACKAGE = "com.stremio.one"

# Standard Android key codes (frameworks/base KeyEvent).
KEY = {
    "up": 19, "down": 20, "left": 21, "right": 22, "select": 23,
    "back": 4, "home": 3,
    "volume_up": 24, "volume_down": 25, "volume_mute": 164,
    "play_pause": 85, "media_play": 126, "media_pause": 127, "media_stop": 86,
    "media_next": 87, "media_previous": 88, "media_forward": 90, "media_rewind": 89,
    "power": 26, "wakeup": 224, "sleep": 223,
}

PLAYBACK_KEY = {
    "play": KEY["media_play"], "pause": KEY["media_pause"], "toggle": KEY["play_pause"],
    "stop": KEY["media_stop"], "next": KEY["media_next"], "previous": KEY["media_previous"],
    "forward": KEY["media_forward"], "rewind": KEY["media_rewind"],
}
NAVIGATE_KEY = {k: KEY[k] for k in ("up", "down", "left", "right", "select", "back", "home")}


class AdbError(RuntimeError):
    """Raised when an adb invocation fails."""


class TVError(RuntimeError):
    """Raised for invalid tool input."""


class AndroidTV:
    """Thin async wrapper around adb for one Android TV target."""

    def __init__(self, host: str, port: int, adb_path: str) -> None:
        if not host:
            raise TVError("ANDROID_TV_HOST is not set; cannot reach the TV.")
        self.target = f"{host}:{port}"
        self.adb_path = adb_path
        self._connected = False

    async def _adb(self, *args: str, timeout: float = 20.0) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.adb_path, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise AdbError(f"adb {' '.join(args)} timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise AdbError(
                f"adb {' '.join(args)} failed ({proc.returncode}): "
                f"{err.decode(errors='replace').strip()}"
            )
        return out.decode(errors="replace")

    async def _shell(self, command: str, timeout: float = 20.0) -> str:
        return await self._adb("-s", self.target, "shell", command, timeout=timeout)

    async def connect(self) -> None:
        if self._connected:
            return
        result = await self._adb("connect", self.target)
        if "connected" not in result.lower():
            raise AdbError(f"could not connect to {self.target}: {result.strip()}")
        self._connected = True

    async def open_uri(self, uri: str) -> None:
        await self._shell(
            f"am start -a android.intent.action.VIEW -d '{uri}' {STREMIO_PACKAGE}"
        )

    async def key(self, keycode: int) -> None:
        await self._shell(f"input keyevent {keycode}")

    async def set_volume(self, level: int) -> None:
        if not 0 <= level <= 15:
            raise TVError("volume level must be between 0 and 15")
        await self._shell(f"media volume --stream 3 --set {level}")

    async def power_state(self) -> str:
        dump = await self._shell("dumpsys power")
        match = re.search(r"mWakefulness=(\w+)", dump)
        return match.group(1) if match else "unknown"

    async def uptime_ms(self) -> Optional[int]:
        try:
            raw = await self._shell("cat /proc/uptime")
        except AdbError:
            return None
        first = raw.strip().split()[0] if raw.strip() else ""
        try:
            return int(float(first) * 1000)
        except ValueError:
            return None

    async def track_duration_ms(self) -> Optional[int]:
        """Longest media.extractor track duration (>= 60s), the played file."""
        try:
            dump = await self._shell("dumpsys media.extractor")
        except AdbError:
            return None
        best = None
        for micros in re.findall(r"durationUs\s*=\s*(\d+)", dump):
            ms = int(micros) // 1000
            if ms >= 60_000 and (best is None or ms > best):
                best = ms
        return best

    async def playback(self) -> dict:
        dump = await self._shell("dumpsys media_session")
        status = _parse_playback(dump)
        if status.get("state") == "playing" and status.get("_anchor") is not None:
            now = await self.uptime_ms()
            if now is not None and status.get("_updated") is not None:
                elapsed = (now - status["_updated"]) * status.get("_speed", 1.0)
                status["position"] = max(0, int(status["_anchor"] + elapsed))
        if status.get("duration") is None and status.get("app"):
            status["duration"] = await self.track_duration_ms()
        for key in ("_anchor", "_updated", "_speed"):
            status.pop(key, None)
        if status.get("duration") and status.get("position") is not None:
            status["position"] = min(status["position"], status["duration"])
        return status


# state=PlaybackState {state=PLAYING(3), position=0, buffered position=0,
#                      speed=1.0, updated=8953146, ...}
_PLAYBACK_RE = re.compile(
    r"state=PlaybackState\s*\{state=(?P<name>\w+)\((?P<code>\d+)\),\s*"
    r"position=(?P<pos>-?\d+),[^}]*?speed=(?P<speed>[-\d.]+),\s*updated=(?P<updated>\d+)"
)
_DESC_RE = re.compile(r"metadata:\s*size=\d+,\s*description=(?P<title>.+)")

_STATE_NAMES = {
    "0": "none", "1": "stopped", "2": "paused", "3": "playing",
    "4": "fast_forwarding", "5": "rewinding", "6": "buffering", "7": "error",
}


def _parse_playback(dump: str) -> dict:
    """Pick the live Stremio session from a media_session dump."""
    best = {"ok": True, "app": None, "state": "stopped", "title": None,
            "position": None, "duration": None}
    for block in _stremio_blocks(dump):
        match = _PLAYBACK_RE.search(block)
        if not match:
            continue
        state = _STATE_NAMES.get(match["code"], match["name"].lower())
        if state in ("error", "none") and best["app"]:
            continue
        title = None
        desc = _DESC_RE.search(block)
        if desc:
            title = desc["title"].strip().rsplit(", null", 1)[0].split(", ")[0].strip() or None
        best = {
            "ok": True, "app": "Stremio", "state": state, "title": title,
            "position": int(match["pos"]), "duration": None,
            "_anchor": int(match["pos"]),
            "_updated": int(match["updated"]),
            "_speed": float(match["speed"]),
        }
        if state in ("playing", "buffering"):
            break
    return best


def _stremio_blocks(dump: str):
    """Yield one text block per Stremio media session (state + metadata together)."""
    lines = dump.splitlines()
    starts = [
        i for i, line in enumerate(lines)
        if STREMIO_PACKAGE in line and "MediaSession" in line
    ]
    if not starts:
        starts = [i for i, line in enumerate(lines) if STREMIO_PACKAGE in line]
    for order, start in enumerate(starts):
        end = starts[order + 1] if order + 1 < len(starts) else start + 24
        yield "\n".join(lines[start:end])


def _video_id(imdb_id: str, content_type: str, season: Optional[int], episode: Optional[int]) -> str:
    if content_type == "series":
        if season is None or episode is None:
            raise TVError("series playback requires both 'season' and 'episode'")
        return f"{imdb_id}:{season}:{episode}"
    return imdb_id


async def _tv() -> AndroidTV:
    tv = AndroidTV(settings.android_tv_host, settings.android_tv_port, settings.adb_path)
    await tv.connect()
    return tv


def register(mcp) -> None:
    @mcp.tool(name="play")
    async def play(
        imdb_id: str,
        content_type: str = "movie",
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> str:
        """Open a movie or series episode in Stremio on the Android TV.

        imdb_id like 'tt1375666'. For series pass season and episode. After the
        detail page opens, presses OK to start the highlighted stream.
        """
        if not re.fullmatch(r"tt\d+", imdb_id or ""):
            return json.dumps({"ok": False, "error": f"invalid imdb_id: {imdb_id!r}"})
        if content_type not in ("movie", "series"):
            return json.dumps({"ok": False, "error": "content_type must be 'movie' or 'series'"})
        try:
            video_id = _video_id(imdb_id, content_type, season, episode)
            tv = await _tv()
            uri = f"stremio:///detail/{content_type}/{imdb_id}/{video_id}"
            await tv.open_uri(uri)
            await asyncio.sleep(2.5)
            await tv.key(KEY["select"])
            return json.dumps({"ok": True, "opened": uri, "video_id": video_id})
        except (AdbError, TVError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    @mcp.tool(name="tv_control")
    async def tv_control(category: str, action: str, value: Optional[int] = None) -> str:
        """Control the TV: category is 'volume' | 'playback' | 'navigate' | 'power'.

        volume: up/down/mute/set (set needs value 0-15).
        playback: play/pause/toggle/stop/next/previous/forward/rewind.
        navigate: up/down/left/right/select/back/home.
        power: wake/sleep/toggle/status.
        """
        try:
            tv = await _tv()
            if category == "volume":
                if action == "set":
                    if value is None:
                        return json.dumps({"ok": False, "error": "volume set requires value 0-15"})
                    await tv.set_volume(value)
                elif action in ("up", "down", "mute"):
                    await tv.key(KEY[f"volume_{action}"])
                else:
                    return json.dumps({"ok": False, "error": f"unknown volume action: {action}"})
            elif category == "playback":
                if action not in PLAYBACK_KEY:
                    return json.dumps({"ok": False, "error": f"unknown playback action: {action}"})
                await tv.key(PLAYBACK_KEY[action])
            elif category == "navigate":
                if action not in NAVIGATE_KEY:
                    return json.dumps({"ok": False, "error": f"unknown navigate action: {action}"})
                await tv.key(NAVIGATE_KEY[action])
            elif category == "power":
                if action == "status":
                    return json.dumps({"ok": True, "power": await tv.power_state()})
                keymap = {"wake": "wakeup", "sleep": "sleep", "toggle": "power"}
                if action not in keymap:
                    return json.dumps({"ok": False, "error": f"unknown power action: {action}"})
                await tv.key(KEY[keymap[action]])
            else:
                return json.dumps({"ok": False, "error": f"unknown category: {category}"})
            return json.dumps({"ok": True, "category": category, "action": action})
        except (AdbError, TVError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    @mcp.tool(name="playback_status")
    async def playback_status() -> str:
        """Report what Stremio is playing on the TV: title, state, position, duration (ms)."""
        try:
            tv = await _tv()
            return json.dumps(await tv.playback())
        except (AdbError, TVError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
