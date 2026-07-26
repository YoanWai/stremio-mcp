"""Settings read from the environment, plus the per-user state directory."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path


def _int_from_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        return int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}") from error


def _default_state_dir() -> Path:
    override = os.getenv("STREMIO_MCP_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if platform.system() == "Windows":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "stremio-mcp"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "stremio-mcp"
    base = os.getenv("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "stremio-mcp"


@dataclass(frozen=True)
class Settings:
    android_tv_host: str = field(default_factory=lambda: os.getenv("ANDROID_TV_HOST", ""))
    android_tv_port: int = field(default_factory=lambda: _int_from_env("ANDROID_TV_PORT", 5555))
    adb_path: str = field(default_factory=lambda: os.getenv("ADB_PATH", "adb"))
    stremio_auth_key: str = field(default_factory=lambda: os.getenv("STREMIO_AUTH_KEY", ""))
    addon_port: int = field(default_factory=lambda: _int_from_env("STREMIO_MCP_ADDON_PORT", 9876))
    state_dir: Path = field(default_factory=_default_state_dir)

    @property
    def auth_cache_path(self) -> Path:
        return self.state_dir / "auth.json"

    @property
    def subtitle_dir(self) -> Path:
        return self.state_dir / "subtitles"


settings = Settings()
