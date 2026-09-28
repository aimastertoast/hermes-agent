"""Regression: dropped-dead-code symbols must not block hermes_cli.update_cmd import.

Background
----------
Upstream commit 143b24c309 ("refactor(update): drop the dead Windows venv-holder
gate") removed the vestigial venv-holder helpers from ``update_cmd_windows.py``:
``_clear_windows_venv_holders_or_exit`` and ``_format_venv_python_holders_message``
(no production callers since PM updates stopped requiring an unoccupied venv).

Our local ``update_cmd.py`` had been frozen with the OLD import list (which still
named those symbols), so every Hermes CLI invocation emitted a WARNING and fell
back to no-op stubs. The fix: drop the dead names from BOTH the import list and
the stub fallback. This test pins that contract so a future patch re-introducing
the old import list is caught at test time, not at runtime.

If upstream RE-ADDS one of these helpers (e.g. resurrects the gate) the test
fails — that's intentional. Update the import list in lockstep.
"""

import io
import contextlib
import subprocess
import sys

import pytest


DEAD_NAMES = (
    "_clear_windows_venv_holders_or_exit",
    "_format_venv_python_holders_message",
)


def _read(path, start_line, end_line):
    """Return ``update_cmd.py`` lines ``start_line..end_line`` (1-indexed, inclusive)."""
    with open(path, encoding="utf-8") as fh:
        all_lines = fh.readlines()
    return "".join(all_lines[start_line - 1:end_line])


def test_update_cmd_does_not_import_dead_venv_holder_names():
    """Both names must be absent from ``update_cmd.py``."""
    from hermes_cli import update_cmd

    src_path = update_cmd.__file__
    src = open(src_path, encoding="utf-8").read()

    for name in DEAD_NAMES:
        assert name not in src, (
            f"{name} still appears in update_cmd.py — upstream dropped it as dead "
            f"code in 143b24c309. Remove from both the import list and the stub "
            f"fallback so the WARNING stops firing."
        )


def test_update_cmd_imports_cleanly_without_warning():
    """``import hermes_cli.update_cmd`` must succeed with no stderr noise.

    Before the fix the import path emitted
        WARNING: hermes_cli.update_cmd_windows symbols not importable ...
    on stderr and downgraded every windows-lifecycle call to a no-op stub.
    """
    # Spawn a clean subprocess so we observe exactly what a fresh Python sees.
    proc = subprocess.run(
        [sys.executable, "-c", "import hermes_cli.update_cmd"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"import hermes_cli.update_cmd failed: {proc.stderr}"
    )
    assert "WARNING" not in proc.stderr, (
        f"update_cmd emitted a WARNING during import — likely a missing symbol "
        f"from update_cmd_windows. stderr:\n{proc.stderr}"
    )
    assert "symbols not importable" not in proc.stderr