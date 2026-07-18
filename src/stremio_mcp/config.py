import os
from dataclasses import dataclass, field


def _port_from_env() -> int:
    raw_value = os.getenv("ANDROID_TV_PORT", "5555")
    try:
        return int(raw_value)
    except ValueError as error:
        raise ValueError(f"ANDROID_TV_PORT must be an integer, got {raw_value!r}") from error


@dataclass(frozen=True)
class Settings:
    android_tv_host: str = field(default_factory=lambda: os.getenv("ANDROID_TV_HOST", ""))
    android_tv_port: int = field(default_factory=_port_from_env)
    adb_path: str = field(default_factory=lambda: os.getenv("ADB_PATH", "adb"))
    stremio_auth_key: str = field(default_factory=lambda: os.getenv("STREMIO_AUTH_KEY", ""))


settings = Settings()
