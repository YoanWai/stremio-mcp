import argparse

from mcp.server.fastmcp import FastMCP

from . import account, addons, tv

mcp = FastMCP("stremio")
addons.register(mcp)
account.register(mcp)
tv.register(mcp)


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="stremio-mcp",
        description="Unified MCP server for Stremio: addon search/streams + Android TV control.",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args()
    if args.version:
        from . import __version__

        print(f"stremio-mcp {__version__}")
        return
    mcp.run()
