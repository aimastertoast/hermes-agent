"""Hermes must pass its SSE event-size floor to the SDK's streamable HTTP client.

mcp 2.0 passes its own 1 MiB default to every ``EventSource`` it opens
(``EventSource(response, max_event_size=self.max_sse_event_size)``), which
defeats the httpx-level ``EventSource`` shim: the shim only widens events the
caller did NOT limit, and the SDK is now the explicit caller. Cloudflare's
~3.7k-tool tools/list arrives as ONE event well past 1 MiB, so the server parks
on "Server-sent event exceeded the 1048576 byte limit". The floor must therefore
travel through ``streamable_http_client(max_sse_event_size=...)`` itself.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import tools.mcp_tool as _mcp
from tools.mcp_tool import MCPServerTask

FLOOR = _mcp._SSE_EVENT_SIZE_FLOOR_BYTES


def _make_task(name: str = "sse-floor-test") -> MCPServerTask:
    """Minimal MCPServerTask without running the heavy __init__."""
    task = MCPServerTask.__new__(MCPServerTask)
    task.name = name
    task._http_rejection = {}
    return task


def test_sdk_streamable_client_accepts_max_sse_event_size():
    """The capability check matches the installed SDK's real signature."""
    import importlib
    import inspect

    fn = getattr(importlib.import_module("mcp.client.streamable_http"),
                 "streamable_http_client", None)
    expected = fn is not None and "max_sse_event_size" in inspect.signature(fn).parameters
    assert _mcp._streamable_http_accepts("max_sse_event_size") is expected
    assert _mcp._streamable_http_accepts("not_a_real_kwarg") is False


def test_streamable_http_transport_passes_sse_event_floor(monkeypatch):
    """The transport hands the SDK the 64 MiB floor via ``max_sse_event_size``.

    SDKs without the kwarg have no per-event cap at all (httpx2 <= 2.7.0), so
    there is nothing to pass — skip rather than assert.
    """
    if not _mcp._streamable_http_accepts("max_sse_event_size"):
        pytest.skip("installed mcp SDK has no max_sse_event_size (no cap either)")
    task = _make_task()
    captured: dict = {}

    @asynccontextmanager
    async def _fake_streams(url, http_client=None, **kwargs):
        captured["kwargs"] = kwargs
        yield (None, None, None)

    monkeypatch.setattr(_mcp, "streamable_http_client", _fake_streams)

    async def _drive():
        cm = task._streamable_http_transport(
            "http://127.0.0.1:9/mcp",
            {},
            connect_timeout=5.0, ssl_verify=True, client_cert=None,
            oauth_auth=None, strict_cfg_headers=True,
            configured_header_names=set())
        async with cm:
            pass

    asyncio.run(_drive())
    assert captured["kwargs"].get("max_sse_event_size") == FLOOR
