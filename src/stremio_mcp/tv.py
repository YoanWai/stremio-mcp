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
import shlex
import tempfile
from pathlib import Path

from .config import settings
from .net import HttpError, get_bytes

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
        self.host = host
        self.target = f"{host}:{port}"
        self.adb_path = adb_path
        self._connected = False

    async def _adb(self, *args: str, timeout: float = 20.0) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.adb_path,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AdbError(f"could not start {self.adb_path}: {exc}") from exc
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

    async def pair(self, pairing_port: int, pairing_code: str) -> str:
        if not 1 <= pairing_port <= 65535:
            raise TVError("pairing_port must be between 1 and 65535")
        if not re.fullmatch(r"\d{6}", pairing_code):
            raise TVError("pairing_code must be exactly six digits")
        target = f"{self.host}:{pairing_port}"
        result = await self._adb("pair", target, pairing_code)
        if "successfully paired" not in result.lower():
            raise AdbError(f"adb did not confirm pairing with {target}: {result.strip()}")
        return target

    async def open_uri(self, uri: str) -> None:
        await self._shell(
            f"am start -a android.intent.action.VIEW -d {shlex.quote(uri)} {STREMIO_PACKAGE}"
        )

    async def key(self, keycode: int) -> None:
        await self._shell(f"input keyevent {keycode}")

    async def push(self, local: str, remote: str) -> None:
        await self._adb("-s", self.target, "push", local, remote)

    async def shell_cmd(self, command: str, timeout: float = 10.0) -> str:
        return await self._shell(command, timeout=timeout)

    async def clear_default_player(self) -> None:
        """Forget the app Stremio hands video off to, so Android asks again.

        Only the default-handler association is cleared. Wiping app data here
        would also destroy the TV's Stremio login and library cache.
        """
        await self._shell(
            f"cmd package clear-defaults {STREMIO_PACKAGE} 2>/dev/null; true"
        )

    async def volume(self) -> dict:
        """Current music-stream volume and the device's own range."""
        raw = await self._shell("media volume --stream 3 --get")
        match = re.search(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]", raw)
        if not match:
            raise TVError(f"could not read the volume from: {raw.strip()!r}")
        return {"level": int(match[1]), "min": int(match[2]), "max": int(match[3])}

    async def set_volume(self, level: int) -> dict:
        limits = await self.volume()
        if not limits["min"] <= level <= limits["max"]:
            raise TVError(f"volume must be between {limits['min']} and {limits['max']}")
        await self._shell(f"media volume --stream 3 --set {level}")
        return {**limits, "level": level}

    async def device_info(self) -> dict:
        raw = await self._shell(
            "getprop ro.product.model; getprop ro.product.manufacturer; "
            "getprop ro.build.version.release; "
            f"dumpsys package {STREMIO_PACKAGE} | grep -m1 versionName"
        )
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        version = next((line.split("=", 1)[-1] for line in lines if "versionName" in line), None)
        return {
            "model": lines[0] if len(lines) > 0 else None,
            "manufacturer": lines[1] if len(lines) > 1 else None,
            "android_version": lines[2] if len(lines) > 2 else None,
            "stremio_version": version,
        }

    async def type_text(self, text: str) -> None:
        await self._shell(f"input text {shlex.quote(text.replace(' ', '%s'))}")

    async def screenshot(self, local_path: str) -> None:
        remote = "/sdcard/stremio-mcp-screen.png"
        await self._shell(f"screencap -p {remote}")
        await self._adb("-s", self.target, "pull", remote, local_path, timeout=40.0)
        await self._shell(f"rm -f {remote}")

    async def power_state(self) -> str:
        dump = await self._shell("dumpsys power")
        match = re.search(r"mWakefulness=(\w+)", dump)
        return match.group(1) if match else "unknown"

    async def uptime_ms(self) -> int | None:
        try:
            raw = await self._shell("cat /proc/uptime")
        except AdbError:
            return None
        first = raw.strip().split()[0] if raw.strip() else ""
        try:
            return int(float(first) * 1000)
        except ValueError:
            return None

    async def track_duration_ms(self) -> int | None:
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


def _video_id(imdb_id: str, content_type: str, season: int | None, episode: int | None) -> str:
    if content_type == "series":
        if season is None or episode is None:
            raise TVError("series playback requires both 'season' and 'episode'")
        return f"{imdb_id}:{season}:{episode}"
    return imdb_id


_device: AndroidTV | None = None


async def _tv() -> AndroidTV:
    """Reuse one connected device across tool calls; adb connect is a round trip."""
    global _device
    if _device is None:
        _device = AndroidTV(settings.android_tv_host, settings.android_tv_port, settings.adb_path)
    try:
        await _device.connect()
    except AdbError:
        _device = None
        raise
    return _device


def register(mcp) -> None:
    @mcp.tool(name="play")
    async def play(
        imdb_id: str,
        content_type: str = "movie",
        season: int | None = None,
        episode: int | None = None,
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
    async def tv_control(category: str, action: str, value: int | None = None) -> str:
        """Control the TV: category is 'volume' | 'playback' | 'navigate' | 'power'.

        volume: up/down/mute/status, or set with value in the device's own range.
        playback: play/pause/toggle/stop/next/previous/forward/rewind.
        navigate: up/down/left/right/select/back/home.
        power: wake/sleep/toggle/status.
        """
        try:
            tv = await _tv()
            if category == "volume":
                if action == "status":
                    return json.dumps({"ok": True, **await tv.volume()})
                if action == "set":
                    if value is None:
                        return json.dumps({"ok": False, "error": "volume set requires a value"})
                    return json.dumps({"ok": True, **await tv.set_volume(value)})
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

    @mcp.tool(name="push_subtitle")
    async def push_subtitle(
        subtitle_url_or_path: str,
        filename: str = "subtitle.srt",
        clear_player_default: bool = False,
    ) -> str:
        """Push an SRT subtitle file to the Android TV Downloads folder.

        subtitle_url_or_path: URL to download the SRT from, or a local absolute path.
        filename: name for the file on the TV (must end in .srt).
        Set clear_player_default to also forget Stremio's chosen video app, so
        Android asks again on the next play and an external player like VLC can
        take over. For subtitles inside Stremio itself, use add_subtitle.
        """
        if not filename.endswith(".srt") or "/" in filename:
            return json.dumps({"ok": False, "error": "filename must be a bare name ending in .srt"})
        try:
            if subtitle_url_or_path.startswith(("http://", "https://")):
                data = await get_bytes(subtitle_url_or_path, max_bytes=4_000_000)
            else:
                data = Path(subtitle_url_or_path).expanduser().read_bytes()
        except (HttpError, OSError) as exc:
            return json.dumps({"ok": False, "error": f"could not read the subtitle: {exc}"})
        if not data.strip():
            return json.dumps({"ok": False, "error": "the subtitle file is empty"})

        local_path = None
        try:
            tv = await _tv()
            with tempfile.NamedTemporaryFile(suffix=".srt", delete=False) as tmp:
                tmp.write(data)
                local_path = tmp.name
            remote = f"/sdcard/Download/{filename}"
            await tv.push(local_path, remote)
            if clear_player_default:
                await tv.clear_default_player()
        except (AdbError, TVError, OSError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        finally:
            if local_path:
                Path(local_path).unlink(missing_ok=True)
        return json.dumps(
            {
                "ok": True,
                "remote_path": remote,
                "size": len(data),
                "player_default_cleared": clear_player_default,
            }
        )

    @mcp.tool(name="playback_status")
    async def playback_status() -> str:
        """Report what Stremio is playing on the TV: title, state, position, duration (ms)."""
        try:
            tv = await _tv()
            return json.dumps(await tv.playback())
        except (AdbError, TVError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    @mcp.tool(name="tv_status")
    async def tv_status() -> str:
        """Report the TV: model, Android version, installed Stremio version, power and volume.

        Run this first when a TV tool misbehaves; it proves whether adb can reach
        the device at all.
        """
        try:
            tv = await _tv()
            info = await tv.device_info()
            return json.dumps(
                {
                    "ok": True,
                    "target": tv.target,
                    **info,
                    "power": await tv.power_state(),
                    "volume": await tv.volume(),
                }
            )
        except (AdbError, TVError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    @mcp.tool(name="tv_type_text")
    async def tv_type_text(text: str) -> str:
        """Type text into whatever field is focused on the TV, such as Stremio's search box."""
        if not text.strip():
            return json.dumps({"ok": False, "error": "text is empty"})
        try:
            tv = await _tv()
            await tv.type_text(text)
        except (AdbError, TVError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, "typed": text})

    @mcp.tool(name="tv_screenshot")
    async def tv_screenshot(save_path: str = "") -> str:
        """Capture what the TV is showing and save it as a PNG on this machine.

        Use it to see which item the remote is sitting on before pressing select.
        """
        target = Path(save_path).expanduser() if save_path else Path(tempfile.gettempdir()) / "stremio-tv.png"
        try:
            tv = await _tv()
            target.parent.mkdir(parents=True, exist_ok=True)
            await tv.screenshot(str(target))
        except (AdbError, TVError, OSError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        if not target.is_file():
            return json.dumps({"ok": False, "error": "adb pull produced no file"})
        return json.dumps({"ok": True, "path": str(target), "size": target.stat().st_size})
