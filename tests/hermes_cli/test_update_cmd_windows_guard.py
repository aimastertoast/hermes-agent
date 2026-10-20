"""The updater must stay importable when ``hermes_cli.update_cmd_windows`` cannot be.

``hermes update`` merges upstream BEFORE it syncs dependencies. If upstream adds an import
to ``hermes_cli/update_cmd_windows.py`` that this checkout has not installed yet, the
module raises ImportError -- and ``hermes_cli/update_cmd.py`` imports that module at the top
level with upstream's own import statement. Without a guard the updater cannot be imported,
so it cannot update itself: the update dies with "Update failed (exit 1)".

The guard lives in the fork-owned ``hermes_cli/update_cmd_fork.py``: it publishes a stub
module under the real name in ``sys.modules``, so upstream's import statement resolves
against the stub and the update continues (minus the Windows gateway lifecycle).

Runs in a subprocess: the guard manipulates ``sys.modules``, which must not leak into the
rest of the test session.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_PROBE = textwrap.dedent(
    """
    import sys

    class _Blocker:
        def find_spec(self, name, path=None, target=None):
            if name == "hermes_cli.update_cmd_windows":
                raise ImportError("blocked for the test")
            return None

    sys.meta_path.insert(0, _Blocker())

    # The fork-owned module publishes the stub ...
    import hermes_cli.update_cmd_fork as fork
    stub = sys.modules.get("hermes_cli.update_cmd_windows")
    assert stub is not None, "no stub published under the real module name"

    # ... and upstream's own import statement resolves against it.
    from hermes_cli.update_cmd_windows import (
        _HOLDER_VALUE_FLAGS_FALLBACK, _holder_value_flags, _resume_windows_gateways_after_update,
    )
    assert _holder_value_flags() is None, "the fallback must be a no-op"
    assert _HOLDER_VALUE_FLAGS_FALLBACK == frozenset(), "the fallback must be an empty set"
    assert _resume_windows_gateways_after_update() is None

    # A symbol upstream added or renamed resolves too (PEP 562), so a future upstream
    # import does not brick the updater either.
    assert stub._some_symbol_upstream_added_later() is None

    # The whole updater module still imports.
    import hermes_cli.update_cmd as update_cmd
    assert update_cmd._holder_value_flags is not None
    assert callable(update_cmd._cmd_update_via_orchestrator)
    print("OK")
    """
)


def _run_probe() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, cwd=str(REPO),
    )


def test_updater_still_imports_when_update_cmd_windows_cannot() -> None:
    """A broken ``update_cmd_windows`` degrades the gateway lifecycle, not the updater."""
    result = _run_probe()
    assert result.returncode == 0, (
        f"the updater could not be imported with a broken update_cmd_windows:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout, result.stdout


def test_the_real_windows_module_is_untouched() -> None:
    """The guard must not touch the real module when it imports fine."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import hermes_cli.update_cmd_fork as f, sys;"
         "m = sys.modules['hermes_cli.update_cmd_windows'];"
         "assert m.__name__ == 'hermes_cli.update_cmd_windows';"
         "assert not hasattr(m, '__getattr__'), 'the real module must not be stubbed';"
         "print('OK')"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, cwd=str(REPO),
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "OK" in result.stdout, result.stdout
