"""Drive the Stremio desktop app on macOS, Windows and Linux.

Three things live here: finding and launching the installed app, talking to the
streaming server it runs on ``127.0.0.1:11470``, and downloading the right
installer when Stremio is not installed yet. Addons themselves are installed
through the account (see :mod:`addon_collection`), which is what makes a laptop
pick them up regardless of platform.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .net import HttpError, client, get_json

STREAMING_SERVER_URL = "http://127.0.0.1:11470"
DOWNLOADS_PAGE = "https://www.stremio.com/downloads"
FLATHUB_PAGE = "https://flathub.org/apps/details/com.stremio.Stremio"
_DOWNLOAD_LINK_RE = re.compile(r"https://dl\.strem\.io/[^\"'\s<>]+")

_MAC_APP = Path("/Applications/Stremio.app")
_WINDOWS_CANDIDATES = (
    r"%LOCALAPPDATA%\Programs\LNV\Stremio-4\stremio.exe",
    r"%PROGRAMFILES%\Stremio\stremio.exe",
    r"%PROGRAMFILES(X86)%\Stremio\stremio.exe",
    r"%LOCALAPPDATA%\Programs\Stremio\Stremio.exe",
)
_LINUX_COMMANDS = ("stremio", "com.stremio.Stremio")

_INSTALLER_RULES = {
    # platform -> (required url fragment, file suffix, next-step hint)
    "Darwin": ("stremio-shell-macos", ".dmg", "Open the .dmg and drag Stremio into Applications."),
    "Windows": ("stremio-shell-ng", ".exe", "Run the downloaded installer."),
    "Linux": ("shell-linux", ".deb", "sudo apt install ./<file>, or use Flatpak instead."),
}


class DesktopError(RuntimeError):
    """Raised when the desktop app cannot be located, launched or reached."""


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def app_path() -> Path | None:
    """Locate the installed Stremio desktop app for the running OS."""
    system = platform.system()
    if system == "Darwin":
        return _MAC_APP if _MAC_APP.is_dir() else None
    if system == "Windows":
        for candidate in _WINDOWS_CANDIDATES:
            expanded = _expand(candidate)
            if expanded.is_file():
                return expanded
        return None
    for command in _LINUX_COMMANDS:
        found = shutil.which(command)
        if found:
            return Path(found)
    flatpak = shutil.which("flatpak")
    if flatpak:
        return Path(flatpak)
    return None


def data_dir() -> Path:
    """Where the desktop app keeps its streaming-server cache and settings."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "stremio-server"
    if system == "Windows":
        return _expand(r"%LOCALAPPDATA%") / "stremio-server"
    return Path(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    ) / "stremio-server"


async def _launch(argument: str | None = None) -> None:
    """Start the desktop app, optionally handing it a stremio:// deep link."""
    system = platform.system()
    target = app_path()
    if target is None:
        raise DesktopError(
            "Stremio desktop is not installed. Run stremio_desktop_download to get the installer."
        )
    if system == "Darwin":
        command = ["open", "-a", str(target)] + (["--args", argument] if argument else [])
    elif system == "Windows":
        command = [str(target)] + ([argument] if argument else [])
    elif target.name == "flatpak":
        command = [str(target), "run", "com.stremio.Stremio"] + ([argument] if argument else [])
    else:
        command = [str(target)] + ([argument] if argument else [])
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise DesktopError(f"could not launch {command[0]}: {error}") from error
    if system == "Darwin":
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise DesktopError(stderr.decode(errors="replace").strip() or "open failed")


async def _open_deep_link(uri: str) -> None:
    """Hand a stremio:// URI to the OS so the running app takes it."""
    system = platform.system()
    if system == "Darwin":
        opener = ["open", uri]
    elif system == "Windows":
        opener = ["cmd", "/c", "start", "", uri]
    else:
        handler = shutil.which("xdg-open")
        if handler is None:
            await _launch(uri)
            return
        opener = [handler, uri]
    process = await asyncio.create_subprocess_exec(
        *opener, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise DesktopError(stderr.decode(errors="replace").strip() or f"could not open {uri}")


def _arch_token(target_platform: str = "") -> str:
    """Architecture slug as it appears in Stremio's installer file names."""
    if target_platform and target_platform != platform.system():
        return "x64"
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


async def installer_links() -> list[str]:
    """Every desktop installer URL listed on the official downloads page."""
    http = await client()
    try:
        response = await http.get(DOWNLOADS_PAGE)
    except httpx.HTTPError as error:
        raise HttpError(f"could not reach {DOWNLOADS_PAGE}: {error}") from error
    if response.status_code != 200:
        raise HttpError(f"HTTP {response.status_code} from {DOWNLOADS_PAGE}")
    found = dict.fromkeys(_DOWNLOAD_LINK_RE.findall(response.text))
    return [link for link in found if "/android/" not in link]


async def server_settings() -> dict[str, Any]:
    return await get_json(f"{STREAMING_SERVER_URL}/settings")


async def server_running() -> bool:
    try:
        await server_settings()
    except HttpError:
        return False
    return True


def register(mcp) -> None:
    @mcp.tool()
    async def stremio_desktop_status() -> str:
        """Report whether Stremio desktop is installed and running on this machine.

        Works on macOS, Windows and Linux. The streaming server is the local
        component that turns torrents into playable HTTP streams; it only answers
        while the desktop app is open.
        """
        target = app_path()
        settings_payload: dict[str, Any] | None = None
        try:
            settings_payload = await server_settings()
        except HttpError:
            settings_payload = None
        cache = data_dir()
        return json.dumps(
            {
                "ok": True,
                "platform": platform.system(),
                "installed": target is not None,
                "app_path": str(target) if target else None,
                "streaming_server": STREAMING_SERVER_URL,
                "streaming_server_running": settings_payload is not None,
                "server_version": (settings_payload or {}).get("values", {}).get("serverVersion")
                or (settings_payload or {}).get("serverVersion"),
                "data_dir": str(cache),
                "data_dir_exists": cache.is_dir(),
            },
            ensure_ascii=False,
        )

    @mcp.tool()
    async def stremio_desktop_launch(wait_seconds: int = 20) -> str:
        """Launch Stremio desktop on this machine and wait for its streaming server.

        Run this before asking the desktop app to play something, and after
        installing addons so the app pulls the new collection from the account.
        """
        try:
            await _launch()
        except DesktopError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        deadline = max(0, wait_seconds)
        for _ in range(deadline):
            if await server_running():
                return json.dumps({"ok": True, "launched": True, "streaming_server_running": True})
            await asyncio.sleep(1)
        return json.dumps(
            {
                "ok": True,
                "launched": True,
                "streaming_server_running": False,
                "note": f"app started but the streaming server was not up within {deadline}s",
            }
        )

    @mcp.tool()
    async def stremio_desktop_open(
        imdb_id: str = "",
        content_type: str = "movie",
        season: int = 0,
        episode: int = 0,
        page: str = "",
    ) -> str:
        """Open a title, or a named page, in Stremio desktop on this machine.

        Pass an imdb_id like 'tt1375666' to open its detail page, adding season
        and episode for a series. Or pass page as one of 'addons', 'library',
        'board', 'search', 'settings' to jump straight there.
        """
        if page:
            allowed = {"addons", "library", "board", "search", "settings", "discover"}
            if page not in allowed:
                return json.dumps({"ok": False, "error": f"page must be one of {sorted(allowed)}"})
            uri = f"stremio:///{page}"
        elif imdb_id:
            if content_type not in ("movie", "series"):
                return json.dumps({"ok": False, "error": "content_type must be 'movie' or 'series'"})
            video_id = imdb_id
            if content_type == "series":
                if not (season and episode):
                    return json.dumps({"ok": False, "error": "series needs season and episode"})
                video_id = f"{imdb_id}:{season}:{episode}"
            uri = f"stremio:///detail/{content_type}/{imdb_id}/{video_id}"
        else:
            return json.dumps({"ok": False, "error": "pass either imdb_id or page"})
        if app_path() is None:
            return json.dumps({"ok": False, "error": "Stremio desktop is not installed"})
        try:
            if not await server_running():
                await _launch()
                for _ in range(20):
                    if await server_running():
                        break
                    await asyncio.sleep(1)
            await _open_deep_link(uri)
        except DesktopError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, "opened": uri})

    @mcp.tool()
    async def stremio_desktop_show_addon(manifest_url: str, content_type: str = "movie") -> str:
        """Open an addon's install page in Stremio desktop so it can be reviewed.

        Use this for addons that need configuring by hand. For a headless install
        that also reaches every other device, use stremio_install_addon instead.
        """
        from .addon_collection import CollectionError, normalize_manifest_url

        try:
            url = normalize_manifest_url(manifest_url)
            manifest = await get_json(url)
        except (CollectionError, HttpError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        addon_id = manifest.get("id")
        if not addon_id:
            return json.dumps({"ok": False, "error": f"{url} is not a Stremio manifest (no id)"})
        base = quote(url.removesuffix("/manifest.json") + "/", safe="")
        uri = f"stremio:///addons/{quote(content_type, safe='')}/{base}/{quote(addon_id, safe='')}"
        try:
            await _open_deep_link(uri)
        except DesktopError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, "opened": uri, "addon": manifest.get("name")})

    @mcp.tool()
    async def stremio_desktop_download(destination: str = "", target_platform: str = "") -> str:
        """Download the Stremio desktop installer for this machine's OS and CPU.

        Reads the official downloads page, picks the macOS .dmg, the Windows .exe
        or the Linux .deb that matches the architecture, and saves it to the
        Downloads folder. Pass target_platform as 'Darwin', 'Windows' or 'Linux'
        to fetch an installer for a different machine. Running the installer is
        left to you because it needs an admin password.
        """
        system = target_platform or platform.system()
        rule = _INSTALLER_RULES.get(system)
        if rule is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"unsupported platform {system!r}",
                    "supported": sorted(_INSTALLER_RULES),
                }
            )
        fragment, suffix, next_step = rule
        try:
            links = await installer_links()
        except HttpError as exc:
            return json.dumps({"ok": False, "error": f"could not read the downloads page: {exc}"})
        candidates = [link for link in links if fragment in link and link.split("?")[0].endswith(suffix)]
        if not candidates:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"the downloads page lists no {suffix} build for {system}",
                    "downloads_page": DOWNLOADS_PAGE,
                    "flatpak": FLATHUB_PAGE if system == "Linux" else None,
                }
            )
        wanted_arch = _arch_token(target_platform)
        preferred = [link for link in candidates if wanted_arch in link.lower()]
        url = (preferred or candidates)[0]

        folder = Path(destination).expanduser() if destination else Path.home() / "Downloads"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return json.dumps({"ok": False, "error": f"cannot write to {folder}: {error}"})
        target = folder / url.split("?")[0].rsplit("/", 1)[-1]

        http = await client()
        try:
            async with http.stream("GET", url) as response:
                if response.status_code != 200:
                    return json.dumps({"ok": False, "error": f"HTTP {response.status_code} for {url}"})
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes(1 << 16):
                        handle.write(chunk)
        except (OSError, HttpError) as error:
            target.unlink(missing_ok=True)
            return json.dumps({"ok": False, "error": f"download failed: {error}"})
        return json.dumps(
            {
                "ok": True,
                "platform": system,
                "arch": wanted_arch,
                "source": url,
                "path": str(target),
                "size": target.stat().st_size,
                "next_step": next_step,
            },
            ensure_ascii=False,
        )
