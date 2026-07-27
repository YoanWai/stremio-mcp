"""MCP server entry point.

Tools are registered lazily so that importing the package, or running
``stremio-mcp --version``, has no side effects such as opening a socket.
"""

from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP

_mcp: FastMCP | None = None


def build_server() -> FastMCP:
    """Create the server and register every toolset onto it."""
    from . import (
        account,
        addon_collection,
        addon_configuration,
        addons,
        desktop,
        desktop_play,
        subtitle_addon,
        tv,
    )

    server = FastMCP("stremio")
    for module in (
        addons,
        account,
        addon_collection,
        addon_configuration,
        desktop,
        desktop_play,
        subtitle_addon,
        tv,
    ):
        module.register(server)
    return server


def get_server() -> FastMCP:
    global _mcp
    if _mcp is None:
        _mcp = build_server()
    return _mcp


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="stremio-mcp",
        description=(
            "MCP server for Stremio: content search, account library, addon management "
            "on desktop and Android TV."
        ),
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    args = parser.parse_args()
    if args.version:
        from . import __version__

        print(f"stremio-mcp {__version__}")
        return
    get_server().run()
