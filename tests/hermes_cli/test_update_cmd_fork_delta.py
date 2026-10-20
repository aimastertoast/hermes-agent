"""The fork's delta on the upstream-owned ``update_cmd.py`` must stay tiny — FORK-OWNED.

``hermes update`` merges ``origin/main`` into the fork's branch and ABORTS on any conflict
(exit 1 — the Desktop's "Update failed (exit 1)" banner). A conflict is per-HUNK, so every
line the fork keeps in an upstream-owned file is a line upstream has to edit in the same
place to stop the update. The fork's code therefore lives in fork-owned modules
(``hermes_cli/update_cmd_fork.py``, ``hermes_cli/update_transient_retry.py``) and only
thin call sites stay in ``hermes_cli/update_cmd.py``.

Measured against the merge base of HEAD and ``origin/main`` — the base the next merge will
actually use. Measuring against ``origin/main`` itself would count upstream's own newer
commits as if the fork had removed those lines. The working tree is measured, not just
HEAD, so the ratchet also covers work in progress.

This test is the ratchet: it fails when the delta grows back, and says where the new code
belongs.
"""

from __future__ import annotations

import ast
import subprocess
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TARGET = "hermes_cli/update_cmd.py"
GIT_TARGET = "hermes_cli/update_cmd_git.py"

# The budget. Every line here is a line upstream has to collide with.
MAX_ADDED = 70
# update_cmd.py is NOT listed ``merge=union`` (see .gitattributes for the measured reason:
# the union driver turns upstream's neighbouring MODIFICATION into a duplicate line, and next
# to a parenthesised import that is a SyntaxError nobody would catch -- ``hermes_cli.update_cmd``
# is not in the updater's ``_UPDATE_CRITICAL_MODULES``). A deleted line is still forbidden for
# the ordinary reason: it is a line upstream has to restore in the same hunk to collide.
MAX_DELETED = 0

# ``update_cmd_git.py`` carries the same rule: the fork's git-timeout work lives in
# ``hermes_cli/update_git_timeout.py`` and only the installer call stays in the file.
GIT_MAX_ADDED = 20
GIT_MAX_DELETED = 0

# Functions the fork moved OUT of the upstream-owned file. Re-defining one of them inline
# puts fork code back into a merge-conflict magnet.
MOVED_OUT = (
    "_cmd_update_via_orchestrator",
    "_orchestrator_outcome_to_exit_code",
    "_remote_has_branch",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    # timeout: a treeless partial clone can make a plain diff hang on a lazy-fetch, and a
    # hanging ratchet is worse than a failing one.
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False,
        timeout=60,
    )


@cache
def _fork_delta(path: str = TARGET) -> tuple[int, int] | None:
    """``(added, deleted)`` for the fork's delta on ``path``, or None without a ref."""
    ref = next(
        (r for r in ("origin/main",) if _git("rev-parse", "--verify", "--quiet", r).returncode == 0),
        None,
    )
    if ref is None:
        return None
    base = _git("merge-base", "HEAD", ref).stdout.strip()
    if not base:
        return None
    result = _git("diff", "--numstat", base, "--", path)
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, changed = parts
        if changed == path and added.isdigit() and deleted.isdigit():
            return int(added), int(deleted)
    return None


def test_update_cmd_fork_delta_stays_within_budget() -> None:
    """The fork's delta on ``update_cmd.py`` must not grow back into a conflict magnet."""
    delta = _fork_delta()
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= MAX_DELETED, (
        f"{TARGET}: the fork now DELETES {deleted} line(s) of upstream's file (budget "
        f"{MAX_DELETED}). Every deleted line is a line upstream has to restore in the same "
        f"hunk to collide. Move the fork's change into a fork-owned module."
    )
    assert added <= MAX_ADDED, (
        f"{TARGET}: the fork now adds {added} line(s) to upstream's file (budget {MAX_ADDED}). "
        f"Every added line is a hunk upstream has to edit in the same place to collide. Move "
        f"the new code into a fork-owned module (hermes_cli/update_cmd_fork.py) and leave a "
        f"one-line call site behind."
    )


def test_update_cmd_git_fork_delta_stays_within_budget() -> None:
    """The fork's delta on ``update_cmd_git.py`` must stay a pure insertion.

    The git-timeout work lives in ``hermes_cli/update_git_timeout.py``; only the installer
    call stays here. A rewritten ``_git_run`` is a line upstream has to edit in the same hunk
    to collide.
    """
    delta = _fork_delta(GIT_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= GIT_MAX_DELETED, (
        f"{GIT_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file (budget "
        f"{GIT_MAX_DELETED}). Every deleted line is a line upstream has to restore in the same "
        f"hunk to collide. Move the fork's change into a fork-owned module."
    )
    assert added <= GIT_MAX_ADDED, (
        f"{GIT_TARGET}: the fork now adds {added} line(s) to upstream's file (budget "
        f"{GIT_MAX_ADDED}). Move the new code into hermes_cli/update_git_timeout.py and leave "
        f"the installer call behind."
    )


def test_moved_fork_functions_are_not_redefined_in_update_cmd() -> None:
    """The fork's moved-out functions must stay imported, not re-defined inline."""
    tree = ast.parse((REPO / TARGET).read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in MOVED_OUT:
        assert name not in defined, (
            f"{name} is defined inside {TARGET} again. It belongs in "
            f"hermes_cli/update_cmd_fork.py: a definition in an upstream-owned file is a "
            f"merge-conflict magnet."
        )


def test_fork_modules_exist() -> None:
    """The fork-owned modules the call sites point at must be present."""
    for path in (
        "hermes_cli/update_cmd_fork.py",
        "hermes_cli/update_transient_retry.py",
        "hermes_cli/update_git_timeout.py",
    ):
        assert (REPO / path).is_file(), f"{path} is missing: the fork's call sites point at it"
