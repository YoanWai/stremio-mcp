"""Shared HTTP helpers for every module that talks to Stremio over the network."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

USER_AGENT = "stremio-mcp"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def client() -> httpx.AsyncClient:
    """Return the process-wide pooled client, creating it on first use."""
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True,
            )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


class HttpError(RuntimeError):
    """Raised when a request fails, returns a non-200, or returns unusable JSON."""


async def get_json(url: str) -> dict[str, Any]:
    http = await client()
    try:
        response = await http.get(url)
    except httpx.HTTPError as error:
        raise HttpError(f"Request to {url} failed: {error}") from error
    if response.status_code != 200:
        raise HttpError(f"HTTP {response.status_code} from {url}")
    try:
        payload = response.json()
    except ValueError as error:
        raise HttpError(f"Invalid JSON from {url}: {error}") from error
    if not isinstance(payload, dict):
        raise HttpError(f"Unexpected response shape from {url}: expected object")
    return payload


async def get_bytes(url: str, max_bytes: int = 8_000_000) -> bytes:
    http = await client()
    try:
        response = await http.get(url)
    except httpx.HTTPError as error:
        raise HttpError(f"Request to {url} failed: {error}") from error
    if response.status_code != 200:
        raise HttpError(f"HTTP {response.status_code} from {url}")
    if len(response.content) > max_bytes:
        raise HttpError(f"{url} returned {len(response.content)} bytes, over the {max_bytes} limit")
    return response.content
