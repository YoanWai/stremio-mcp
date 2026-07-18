# stremio-mcp

Unified MCP server for Stremio. Two toolsets in one server:

- **Addon tools**: search, metadata, catalogs, and stream listings via Stremio's public addon HTTP APIs.
- **Android TV tools**: play content and control playback on an Android TV running Stremio, over adb.

## Install

### uvx (recommended)

```bash
uvx stremio-mcp
```

### pip

```bash
pip install stremio-mcp
stremio-mcp
```

### Homebrew

```bash
brew install yoanwai/tap/stremio-mcp
```

## MCP client configuration

```json
{
  "mcpServers": {
    "stremio": {
      "command": "uvx",
      "args": ["stremio-mcp"],
      "env": {
        "ANDROID_TV_HOST": "192.168.1.100",
        "ANDROID_TV_PORT": "5555",
        "STREMIO_AUTH_KEY": ""
      }
    }
  }
}
```

With a `.env` file instead of inline env:

```json
{
  "mcpServers": {
    "stremio": {
      "command": "uvx",
      "args": ["--env-file", "/path/to/.env", "stremio-mcp"]
    }
  }
}
```

## Environment variables

| Variable | Default | Required | Purpose |
|---|---|---|---|
| `ANDROID_TV_HOST` | (none) | For TV tools | IP or hostname of the Android TV |
| `ANDROID_TV_PORT` | `5555` | No | adb TCP port on the TV |
| `ADB_PATH` | `adb` | No | Path to the adb binary |
| `STREMIO_AUTH_KEY` | (none) | For library tools | Stremio account auth key (or use `stremio_login`) |

Copy `.env.example` to `.env` and fill in your values.

## Tools

### Addon tools

| Tool | Description |
|---|---|
| `stremio_search` | Search movies and series across addon catalogs |
| `stremio_get_meta` | Fetch full metadata for a title (seasons, episodes, cast) |
| `stremio_browse_catalog` | Browse an addon catalog (popular, trending, genres) |
| `stremio_get_addon_manifest` | Inspect an addon's manifest and capabilities |
| `stremio_get_streams` | List available streams for a title or episode |

### Account and library tools

| Tool | Description |
|---|---|
| `stremio_login` | Log in with email and password to obtain an auth key |
| `stremio_get_library` | List the account's saved library titles |
| `stremio_add_to_library` | Add a movie or series to the library |
| `stremio_remove_from_library` | Remove a title from the library |

### Android TV tools

| Tool | Description |
|---|---|
| `play` | Open and play a title on the TV's Stremio app |
| `tv_control` | Remote-control actions: playback (play/pause/forward/rewind), navigation, volume, power |
| `playback_status` | Report what is currently playing and its position |

## adb pairing

The TV tools require adb access to the Android TV:

1. On the TV: Settings > Device Preferences > About > tap **Build** 7 times to enable Developer options.
2. In Developer options, enable **USB debugging** (and **Network debugging** where available).
3. From your machine: `adb connect <TV_IP>:5555` and accept the authorization prompt on the TV screen.

The connection persists across reboots on most devices; re-run `adb connect` if the TV stops responding.

## License

MIT, see [LICENSE](LICENSE).
