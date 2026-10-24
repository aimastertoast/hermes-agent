"""Hermes' SSE event-size floor must survive upstream touching the same code.

Upstream now caps every MCP HTTP body and every SSE event at
``tools.mcp_tool_transport._MCP_HTTP_MAX_BODY_BYTES`` (10 MiB) and hands the same cap to
the SDK as ``max_sse_event_size``. The fork's floor (64 MiB) used to be passed at the
transport's call site -- an edit inside an upstream-owned file. Upstream changed those
very lines, the merge conflicted, and the GUI update stopped: "Merge conflict between
local commits and upstream".

So the floor travels as a runtime swap of the transport module's own globals: the fork
edits nothing upstream owns, and upstream can keep changing that file freely. The cap
function's limit is bound as a default argument at import time, so the module global
alone cannot move it -- the function is wrapped to force the floor.

Measured: Cloudflare's tools/list arrives as ONE 3.53 MiB SSE event (2026-10-24).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import tools.mcp_tool as _mcp
from tools.mcp_tool import MCPServerTask

FLOOR = _mcp._SSE_EVENT_SIZE_FLOOR_BYTES
UPSTREAM_CAP = 10 * 1024 * 1024


def _fake_transport_module(cap: int = UPSTREAM_CAP):
    """A stand-in for ``tools.mcp_tool_transport`` with upstream's cap shape."""
    recorded: dict = {}

    def _make_mcp_body_cap_transport(httpx_mod, inner_transport, limit: int = cap):
        recorded["limit"] = limit
        return SimpleNamespace(httpx_mod=httpx_mod, inner=inner_transport)

    return SimpleNamespace(
        _MCP_HTTP_MAX_BODY_BYTES=cap,
        _make_mcp_body_cap_transport=_make_mcp_body_cap_transport,
        recorded=recorded,
    )


def test_the_floor_raises_the_transport_module_cap():
    """Upstream's 10 MiB cap becomes Hermes' floor."""
    transport = _fake_transport_module()

    _mcp._apply_sse_event_size_floor(transport)

    assert transport._MCP_HTTP_MAX_BODY_BYTES == FLOOR


def test_the_wire_cap_forwards_the_floor():
    """The cap function's limit is a default argument bound at import time, so the
    function itself has to carry the floor -- otherwise the wire cap still cuts the
    event before the SDK ever sees it."""
    transport = _fake_transport_module()

    _mcp._apply_sse_event_size_floor(transport)
    transport._make_mcp_body_cap_transport(object(), object())

    assert transport.recorded["limit"] == FLOOR


def test_a_smaller_explicit_limit_is_raised_to_the_floor():
    transport = _fake_transport_module()

    _mcp._apply_sse_event_size_floor(transport)
    transport._make_mcp_body_cap_transport(object(), object(), limit=1024)

    assert transport.recorded["limit"] == FLOOR


def test_a_larger_cap_is_left_alone():
    transport = _fake_transport_module(cap=128 * 1024 * 1024)

    _mcp._apply_sse_event_size_floor(transport)

    assert transport._MCP_HTTP_MAX_BODY_BYTES == 128 * 1024 * 1024


def test_applying_the_floor_twice_is_a_no_op():
    """Two callers racing for the first MCP client must not double-wrap the cap."""
    transport = _fake_transport_module()

    _mcp._apply_sse_event_size_floor(transport)
    patched = transport._make_mcp_body_cap_transport
    _mcp._apply_sse_event_size_floor(transport)

    assert transport._make_mcp_body_cap_transport is patched
    transport._make_mcp_body_cap_transport(object(), object())
    assert transport.recorded["limit"] == FLOOR


def test_a_transport_module_without_the_caps_is_untouched():
    """Upstream may rename or drop the caps; the floor must degrade, not explode."""
    transport = SimpleNamespace()

    _mcp._apply_sse_event_size_floor(transport)

    assert not hasattr(transport, "_MCP_HTTP_MAX_BODY_BYTES")
    assert not hasattr(transport, "_make_mcp_body_cap_transport")


def test_the_sdk_receives_the_floor():
    """End to end: the transport hands the SDK the floor as ``max_sse_event_size``.

    Upstream reads ``_MCP_HTTP_MAX_BODY_BYTES`` from its own module global when it
    builds the SDK call, so raising that global is what the SDK ends up with.
    """
    import tools.mcp_tool_transport as transport

    # Register the originals so pytest restores them after this test.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(transport, "_MCP_HTTP_MAX_BODY_BYTES", transport._MCP_HTTP_MAX_BODY_BYTES)
    monkey.setattr(transport, "_make_mcp_body_cap_transport", transport._make_mcp_body_cap_transport)
    try:
        _mcp._apply_sse_event_size_floor(transport)
        task = MCPServerTask.__new__(MCPServerTask)
        task.name = "sse-floor-test"
        task._http_rejection = {}
        captured: dict = {}

        @asynccontextmanager
        async def _fake_streams(url, http_client=None, **kwargs):
            captured["kwargs"] = kwargs
            yield (None, None, None)

        monkey.setattr(_mcp, "streamable_http_client", _fake_streams)

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
    finally:
        monkey.undo()

    assert captured["kwargs"].get("max_sse_event_size") == FLOOR
