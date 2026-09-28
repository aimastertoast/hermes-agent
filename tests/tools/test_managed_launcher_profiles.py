"""Sub-profiles must resolve bare Node/uv launchers through PM, not a per-profile tree.

Evidence: under HERMES_HOME=<root>/profiles/bobby, bare ``npx`` crashed every
stdio MCP server with WinError 2 because the runtime was looked up in the
profile's own home (2026-09-25, reproduced end-to-end). The old
``<home>/node`` managed tree is now retired from runtime selection, so the
lookup is PM's machine-scoped store — which no sub-profile can miss. These
guard that property: resolution must be identical from a sub-profile home,
must ignore a stale per-profile tree, and must never silently fall back to
the PARENT's PATH.
"""
import os
import sys
import types

import pytest

from tools.mcp_tool_config import _managed_launcher, _resolve_stdio_command


@pytest.fixture
def pm_npm(tmp_path, monkeypatch):
    """A PM-managed npm tree plus stubbed PM bookkeeping, as ``_managed_launcher`` sees it."""
    bin_dir = tmp_path / "pm" / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    npx = bin_dir / ("npx.cmd" if os.name == "nt" else "npx")
    npx.write_text("#!/bin/sh\n", encoding="utf-8")
    npx.chmod(0o755)

    import pm
    import hermes_constants

    package = types.SimpleNamespace(missing_reason=lambda target: None)
    monkeypatch.setattr(pm, "get_package", lambda name: package)
    monkeypatch.setattr(pm, "current_target", lambda: "test")
    monkeypatch.setattr(pm, "ensure", lambda name: None)
    monkeypatch.setattr(
        hermes_constants,
        "with_hermes_node_path",
        lambda env=None: {**(env or {}), "PATH": str(bin_dir)},
    )
    return npx


def _sub_profile(root):
    """HERMES_HOME for a sub-profile, which carries no managed tree of its own."""
    bobby = root / "profiles" / "bobby"
    bobby.mkdir(parents=True)
    return bobby


def test_bare_npx_resolves_from_sub_profile(tmp_path, monkeypatch, pm_npm):
    """The 2026-09-25 regression: a bare launcher resolves under a sub-profile home."""
    monkeypatch.setenv("HERMES_HOME", str(_sub_profile(tmp_path / "root")))

    resolved = _managed_launcher("npx")

    assert resolved is not None
    assert os.path.normcase(resolved[0]) == os.path.normcase(str(pm_npm))


def test_stale_per_profile_tree_is_ignored(tmp_path, monkeypatch, pm_npm):
    """A leftover <profile>/node tree must not shadow the machine-scoped PM copy."""
    bobby = _sub_profile(tmp_path / "root")
    stale = bobby / "node"
    stale.mkdir()
    (stale / ("npx.cmd" if os.name == "nt" else "npx")).write_text("stale", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(bobby))

    resolved = _managed_launcher("npx")

    assert os.path.normcase(resolved[0]) == os.path.normcase(str(pm_npm))


def test_managed_dirs_go_first_on_the_child_path(tmp_path, monkeypatch, pm_npm):
    """npx's ``env node`` re-exec needs PM's dirs ahead of any user copy."""
    monkeypatch.setenv("HERMES_HOME", str(_sub_profile(tmp_path / "root")))

    _, env = _resolve_stdio_command("npx", {"PATH": os.pathsep.join(["/usr/bin", "/bin"])})

    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(str(pm_npm))


def test_no_parent_path_leak_when_pm_has_no_build(tmp_path, monkeypatch):
    """PM ships no build for this platform (Termux): fall through to the CHILD's PATH only."""
    import pm

    package = types.SimpleNamespace(missing_reason=lambda target: "no build for platform")
    monkeypatch.setattr(pm, "get_package", lambda name: package)
    monkeypatch.setattr(pm, "current_target", lambda: "test")
    monkeypatch.setenv("HERMES_HOME", str(_sub_profile(tmp_path / "root")))
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable))

    command, env = _resolve_stdio_command("npx", {"PATH": ""})

    # A miss stays as written so the child fails honestly; it never "resolves" to the parent's.
    assert command == "npx"
    assert "PATH" not in env or env["PATH"] == ""
