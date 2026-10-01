"""The desktop update shim must resolve the parity verifier through the launcher.

Regression test for a real bug: the Windows update shim ran the config-parity /
gateway-liveness / MCP-fingerprint verifier as
``Invoke-HermesStep $pythonExe @("-m", "hermes_cli.update_verification")``.
``$pythonExe`` is PM's bare store interpreter, which has no site-packages, so
``import yaml`` at the top of ``update_verification.py`` raised
``ModuleNotFoundError`` and the whole verification step died before running a
single check. The desktop still reported success (the ASAR-verify and
runtime-file self-heal steps above it are independent and passed), so config
parity / gateway liveness / MCP fingerprints were silently never verified.

The fix routes the step through ``Get-HermesRuntimeCommand`` — the launcher's
command, which activates the dependency generation first — matching how the
ASAR-verify step already resolves its module. This test pins that the verifier
is no longer launched with the bare ``$pythonExe``.

The shim is PowerShell, so the assertion is on its source: the bug and the fix
are both "which interpreter/command launches this module."
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[2] / "scripts" / "desktop-update" / "windows.ps1"

pytestmark = pytest.mark.skipif(not SHIM.is_file(), reason="Windows update shim is not present")


def _shim_text() -> str:
    return SHIM.read_text(encoding="utf-8", errors="replace")


def test_parity_verifier_is_not_launched_with_the_bare_store_python():
    """The post-update parity verifier must not be invoked via bare ``$pythonExe -m``."""
    # The step name is the LAST argument of the Invoke-HermesStep call, so anchor
    # on the call and capture the interpreter expression that precedes the label.
    match = re.search(
        r"Invoke-HermesStep\s+(\S+)\s+[^\r\n]*?\"post-update-verify\"",
        _shim_text(),
    )
    assert match, "could not locate the post-update-verify Invoke-HermesStep call in the shim"
    launcher_expr = match.group(1)
    assert launcher_expr != "$pythonExe", (
        "the post-update parity verifier is launched with bare $pythonExe (PM's store "
        "interpreter, no site-packages); `import yaml` fails and verification is skipped. "
        "Resolve it with Get-HermesRuntimeCommand like the ASAR-verify step does."
    )


def test_parity_verifier_resolves_through_the_runtime_launcher():
    """The verifier module must be resolved via Get-HermesRuntimeCommand."""
    text = _shim_text()
    assert "Get-HermesRuntimeCommand" in text, "shim no longer resolves runtime commands"
    # The parity verifier's module name must be routed through the launcher with
    # the -Module switch (this is what activates the dependency generation).
    assert re.search(
        r"Get-HermesRuntimeCommand[^\n]*-Module\s+'hermes_cli\.update_verification'",
        text,
    ), "update_verification is not resolved via Get-HermesRuntimeCommand -Module"
