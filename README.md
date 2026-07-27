# stremio-mcp

An MCP server for Stremio. It searches for something to watch, manages the
account library, installs and orders addons across every device you are signed
in on, and drives Stremio on a laptop or an Android TV.

Addons are installed through the Stremio account rather than through any one
app's config files. The account is the shared source of truth, so a single
`stremio_install_addon` call reaches Stremio desktop on **macOS, Windows and
Linux**, the Android TV app, mobile and the web client at once.

## Install

```bash
uvx stremio-mcp          # run without installing
pip install stremio-mcp  # or install it
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
        "STREMIO_AUTH_KEY": "",
        "ANDROID_TV_HOST": "192.168.1.100"
      }
    }
  }
}
```

Point at a `.env` file instead with `"args": ["--env-file", "/path/to/.env", "stremio-mcp"]`.

## Environment

| Variable | Default | Needed for | Purpose |
|---|---|---|---|
| `STREMIO_AUTH_KEY` | (none) | account, addon, library tools | Stremio auth key, or call `stremio_login` once |
| `ANDROID_TV_HOST` | (none) | TV tools | IP or hostname of the Android TV |
| `ANDROID_TV_PORT` | `5555` | | adb TCP port on the TV |
| `ADB_PATH` | `adb` | | Path to the adb binary |
| `STREMIO_MCP_ADDON_PORT` | `9876` | subtitle addon | Port the built-in subtitle addon serves on |
| `STREMIO_MCP_STATE_DIR` | platform data dir | | Cached login, subtitle files, addon backups |

Copy `.env.example` to `.env` and fill in what you need. Get an auth key from the
console at [web.stremio.com](https://web.stremio.com):

```js
JSON.parse(localStorage.getItem("profile")).auth.key
```

## Tools

### Find something to watch

| Tool | Description |
|---|---|
| `stremio_search` | Search movies and series by name |
| `stremio_get_meta` | Full metadata for a title, with the episode list for a series |
| `stremio_browse_catalog` | Browse the top catalog, optionally by genre |
| `stremio_get_addon_manifest` | Inspect one addon's manifest and capabilities |
| `stremio_get_streams` | Ask one specific addon for streams |
| `stremio_find_streams` | Ask **every** installed addon at once and merge the results |

### Account and library

| Tool | Description |
|---|---|
| `stremio_login` | Sign in with email and password, optionally remembering the key |
| `stremio_logout` | Forget the stored key |
| `stremio_account_status` | Who is signed in |
| `stremio_get_library` | List library titles, filterable by type and watched state |
| `stremio_continue_watching` | Part-watched titles with resume positions |
| `stremio_add_to_library` | Add a title |
| `stremio_remove_from_library` | Remove a title, keeping its watch state |
| `stremio_mark_watched` | Mark a title watched or unwatched everywhere |

### Addons

Changes here reach every signed-in device, which is how a laptop gets its addons.

| Tool | Description |
|---|---|
| `stremio_list_addons` | List installed addons in the order they are queried |
| `stremio_search_addons` | Search the official addon directory |
| `stremio_install_addon` | Install or upgrade an addon by manifest URL, at a chosen position |
| `stremio_uninstall_addon` | Remove an addon by id or URL |
| `stremio_reorder_addons` | Change which addon answers first for streams |
| `stremio_restore_default_addons` | Reinstall Stremio's seven defaults |

Every write merges against the live collection, keeps Stremio's protected
addons, and snapshots the previous list under the state directory.

### Desktop, on macOS / Windows / Linux

| Tool | Description |
|---|---|
| `stremio_desktop_status` | Whether the app is installed and its streaming server is up |
| `stremio_desktop_launch` | Start the app and wait for its streaming server |
| `stremio_desktop_open` | Open a title, or the addons / library / settings page |
| `stremio_desktop_show_addon` | Open an addon's install page for a configurable addon |
| `stremio_desktop_download` | Download the matching installer for this OS and CPU |
| `stremio_streaming_server_streams` | Active torrent transfers and per-file progress |
| `stremio_streaming_server_cache` | Cache limit, usage and torrent entries |
| `stremio_streaming_server_purge_cache` | Stop engines and purge one torrent or the full cache |

### Subtitles

`add_subtitle` serves a subtitle file from this machine through a small addon,
so it shows up in Stremio's own subtitle picker on any device on the LAN.

| Tool | Description |
|---|---|
| `add_subtitle` | Serve an .srt for a title, or for one episode |
| `list_subtitles` | List what is being served |
| `remove_subtitle` | Stop serving one subtitle or all of a title's |
| `sync_addon` | Install the subtitle addon onto the account |

### Android TV

| Tool | Description |
|---|---|
| `play` | Open and start a title in the TV's Stremio app |
| `tv_control` | Playback, navigation, volume and power |
| `playback_status` | What is playing, with position and duration |
| `tv_status` | Model, Android version, Stremio version, power, volume |
| `tv_type_text` | Type into the focused field, such as the search box |
| `tv_screenshot` | Save a PNG of the TV screen |
| `push_subtitle` | Push an .srt to the TV's Downloads folder for an external player |

## adb pairing for the TV tools

1. On the TV: Settings > Device Preferences > About, then tap **Build** seven times.
2. In Developer options, turn on **USB debugging** and **Network debugging**.
3. Run `adb connect <TV_IP>:5555` and accept the prompt on the TV.

The pairing survives reboots on most devices; re-run `adb connect` if the TV
stops answering.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest
ruff check src tests
```

## License

MIT, see [LICENSE](LICENSE).
