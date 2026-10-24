"""The fork's delta on the upstream-owned ``update_cmd.py`` must stay tiny — FORK-OWNED.

``hermes update`` merges ``origin/main`` into the fork's branch and ABORTS on any conflict
(exit 1 — the Desktop's "Update failed (exit 1)" banner). A conflict is per-HUNK, so every
line the fork keeps in an upstream-owned file is a line upstream has to edit in the same
place to stop the update. The fork's code therefore lives in fork-owned modules
(``hermes_cli/update_cmd_fork.py``, ``hermes_cli/update_transient_retry.py``,
``hermes_cli/update_receipt_fork.py``, ``hermes_cli/source_check_fork.py``,
``apps/desktop/src/app/updates-overlay-fork.tsx``,
``apps/desktop/src/store/update-receipt-fork.ts``) and only
thin call sites stay in the upstream-owned files.

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
RECEIPT_TARGET = "hermes_cli/update_receipt.py"
SOURCE_CHECK_TARGET = "hermes_cli/source_check.py"
SOURCE_CHECK_TEST_TARGET = "tests/hermes_cli/test_source_check.py"

# The Desktop's update overlay must carry NO fork delta either. Upstream keeps
# editing ``apps/desktop/src/app/updates-overlay.tsx`` (release notes, rollback
# copy, channel wording), so the fork's receipt pane lives in
# ``apps/desktop/src/app/updates-overlay-fork.tsx`` instead: it takes the dialog
# over only while the pane applies and renders upstream's overlay otherwise, so
# upstream's UI keeps improving without a re-sync.
DESKTOP_OVERLAY_TARGET = "apps/desktop/src/app/updates-overlay.tsx"
DESKTOP_OVERLAY_MAX_ADDED = 0
DESKTOP_OVERLAY_MAX_DELETED = 0

# The fork's wiring points at the fork's overlay instead of upstream's. That
# ONE import line is the only fork line left in an upstream-owned desktop file.
DESKTOP_WIRING_TARGET = "apps/desktop/src/app/contrib/wiring.tsx"
DESKTOP_WIRING_MAX_ADDED = 1
DESKTOP_WIRING_MAX_DELETED = 1

# ``apps/desktop/src/store/updates.ts`` is upstream's store. The fork's part
# (the two update-settings atoms and the post-apply relaunch fix) lives in the
# fork-owned ``apps/desktop/src/store/updates-fork.ts``; upstream's file keeps
# only a re-export of the atoms (so every existing consumer still imports them
# from ``@/store/updates``) and the two call sites of the relaunch helper.
# The receipt atoms used to live here too; they moved to the fork-owned
# ``apps/desktop/src/store/update-receipt-fork.ts`` (a global "acknowledged"
# boolean was never reset, so it hid every receipt forever -- the server owns
# the per-receipt flag). The budget below is the measured delta AFTER that
# move, so the remaining delta cannot grow back.
DESKTOP_UPDATES_STORE_TARGET = "apps/desktop/src/store/updates.ts"
DESKTOP_UPDATES_STORE_MAX_ADDED = 12
DESKTOP_UPDATES_STORE_MAX_DELETED = 0

# ``gateway/run_adapters.py`` is upstream's adapter lifecycle mixin, and the
# fork's cross-profile lock-conflict parking (auto-recovery on holder PID exit)
# lives in the fork-owned ``gateway/reconnect_lock_fork.py``. Two thin
# delegations stay in the upstream file, so its delta is a pure insertion and a
# routine upstream edit next to them merges cleanly.
RUN_ADAPTERS_TARGET = "gateway/run_adapters.py"
RUN_ADAPTERS_MAX_ADDED = 12
RUN_ADAPTERS_MAX_DELETED = 0

# The Desktop's bridge runs the update check's Python entry. Upstream's entry
# answers a branch that lives only on the fork remote with
# ``error="branch-local-only"``, which the About pane renders as "couldn't
# reach the update server" -- a standstill for every fork install. The bridge
# picks the fork-owned ``hermes_cli/source_check_fork.py`` entry when the
# checkout ships one; the delta is that choice alone and must not grow.
DESKTOP_SOURCE_PROBE_TARGET = "apps/desktop/electron/updater/checkout-source.ts"
DESKTOP_SOURCE_PROBE_MAX_ADDED = 22
DESKTOP_SOURCE_PROBE_MAX_DELETED = 0

# The receipt endpoint's route lives in upstream's ``web_routers/actions.py``.
# The fork's part is the profile-scoped read plus the acknowledge route; the
# readers themselves live in the fork-owned ``hermes_cli/update_receipt_fork.py``.
# The fork's own router (``hermes_cli/web_routers/actions_fork.py``) now SHADOWS
# upstream's, so this file must carry NO fork delta at all.
ACTIONS_ROUTER_TARGET = "hermes_cli/web_routers/actions.py"
ACTIONS_ROUTER_MAX_ADDED = 0
ACTIONS_ROUTER_MAX_DELETED = 0

# ``hermes_cli/web_server.py`` mounts the fork's router BEFORE upstream's so the
# fork's routes win their shared paths. Those mount lines are the fork's ONLY
# footprint in the file (a standalone import + one include, so a union merge
# that duplicates them stays a harmless rebinding / double mount).
WEB_SERVER_TARGET = "hermes_cli/web_server.py"
WEB_SERVER_MAX_ADDED = 6
WEB_SERVER_MAX_DELETED = 0

# ``hermes_cli/venv_sync.py`` is upstream's launch path, and the fork adds ONE
# call there: the Desktop's own commands leave an owed completion tail to
# ``hermes update`` (the decision lives in the fork-owned
# ``hermes_cli/update_desktop_tail.py``). An inline tail holds the update lock,
# and the live marker fences the gateway the Desktop is starting -- the
# Desktop fenced itself and could not start at all. The delta is a pure
# insertion, so a routine upstream edit next to it merges cleanly, and the
# budget keeps it that way.
VENV_SYNC_TARGET = "hermes_cli/venv_sync.py"
VENV_SYNC_MAX_ADDED = 8
VENV_SYNC_MAX_DELETED = 0

# ``hermes_cli/update_windows_acl.py`` is upstream's ACL-repair step, and the fork's
# incremental repair (fork-owned module, one call site) replaces the whole-tree walk that
# cost 22 minutes of every update. The delta is a pure insertion, so a routine upstream
# edit next to it merges cleanly, and the budget keeps it that way.
ACL_TARGET = "hermes_cli/update_windows_acl.py"
ACL_MAX_ADDED = 8
ACL_MAX_DELETED = 0

# ``hermes_cli/update_cmd_windows.py`` is upstream's Windows gateway-lifecycle
# module. The fork's part (the Scheduled-Task relaunch, which spawns outside the
# updater's Job Object, and the post-update gate) lives in the fork-owned
# ``hermes_cli/update_cmd_windows_fork.py``; only the delegation and the fork's
# comment stay in the upstream file. The ONE remaining deletion is upstream's
# original call line, whose TARGET genuinely changed -- a real overlap, so this
# file is deliberately NOT union-listed and must keep its loud conflict.
UPDATE_CMD_WINDOWS_TARGET = "hermes_cli/update_cmd_windows.py"
UPDATE_CMD_WINDOWS_MAX_ADDED = 4
UPDATE_CMD_WINDOWS_MAX_DELETED = 1

# ``.gitignore`` must stay a PURE INSERTION. The fork's only rule that upstream
# does not have is ``qa-reports/``. The fork once dropped upstream's whole
# build-cache block (``.build/``, ``.cache/``, ``/pm/.lock.json.lock``,
# ``apps/desktop/.dist-build*``) in a commit whose message only announced
# ``qa-reports/`` -- and untracked build debris then made the parked-branch
# guard refuse every update. Restoring upstream's rules is the fix; the budget
# keeps the fork from re-deleting them.
GITIGNORE_TARGET = ".gitignore"
GITIGNORE_MAX_ADDED = 3
GITIGNORE_MAX_DELETED = 0

# The files the fork migrated to a pure insertion in one batch. Each of them used to
# DELETE a line of upstream's file -- a conflict magnet -- and now only adds lines.
#
# * ``hermes_cli/banner.py`` is the file upstream rewrites most (54 commits since
#   2026-09-01). The fork's background update check must reach the fork's branch
#   check, and it does it through a DECORATOR, so upstream's own ``def _run():``
#   line stays byte-identical.
# * ``hermes_cli/_startup_fast.py`` is the ``--version`` line. It must stay
#   stdlib-only and cheap, so the fork's import stays inside the function; the name
#   is rebound to the fork's checker right after upstream's own import.
# * ``hermes_cli/web_routers/_common.py`` is the shared write-path plumbing. The
#   fork's fail-closed profile rule is a self-contained block in front of
#   upstream's own ``with`` statements.
# * ``gateway/status_inline_source.py`` collapses the doubled separators a Windows
#   path arrives with. The fork's two lines sit in front of upstream's untouched
#   return.
# * ``apps/desktop/src/plugins/hermes-bots/group-external-writes.ts`` passes the
#   background-resume budget at ONE call site. The ONE remaining deletion is
#   upstream's multi-line call, whose fourth argument genuinely changed -- a real
#   overlap, so this file keeps its loud conflict and is NOT union-listed.
BANNER_TARGET = "hermes_cli/banner.py"
BANNER_MAX_ADDED = 6
BANNER_MAX_DELETED = 0

STARTUP_FAST_TARGET = "hermes_cli/_startup_fast.py"
STARTUP_FAST_MAX_ADDED = 5
STARTUP_FAST_MAX_DELETED = 0

WEB_COMMON_TARGET = "hermes_cli/web_routers/_common.py"
WEB_COMMON_MAX_ADDED = 12
WEB_COMMON_MAX_DELETED = 0

STATUS_INLINE_SOURCE_TARGET = "gateway/status_inline_source.py"
STATUS_INLINE_SOURCE_MAX_ADDED = 6
STATUS_INLINE_SOURCE_MAX_DELETED = 0

GROUP_EXTERNAL_WRITES_TARGET = "apps/desktop/src/plugins/hermes-bots/group-external-writes.ts"
GROUP_EXTERNAL_WRITES_MAX_ADDED = 10
GROUP_EXTERNAL_WRITES_MAX_DELETED = 1

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

# ``update_receipt.py`` must carry NO fork delta at all. Upstream rewrites it constantly,
# so even a pure insertion is a hunk a future merge can trip over: the pipeline receipt
# contract (the typed ``UpdateReceiptRecord``, the atomic write/read/acknowledge helpers,
# the verification section) lives in ``hermes_cli/update_receipt_fork.py`` instead.
RECEIPT_MAX_ADDED = 0
RECEIPT_MAX_DELETED = 0

# ``hermes_cli/source_check.py`` and its test file must carry NO fork delta either.
# The fork's update-check behavior (a local-only branch is a pin, not an error) lives in
# ``hermes_cli/source_check_fork.py``; upstream's file must stay byte-identical so the
# merge never has to touch the branch-comparison code upstream keeps rewriting.
SOURCE_CHECK_MAX_ADDED = 0
SOURCE_CHECK_MAX_DELETED = 0

# Functions the fork moved OUT of the upstream-owned file. Re-defining one of them inline
# puts fork code back into a merge-conflict magnet.
MOVED_OUT = (
    "_cmd_update_via_orchestrator",
    "_orchestrator_outcome_to_exit_code",
    "_remote_has_branch",
)

# The pipeline receipt contract, moved out of ``hermes_cli/update_receipt.py``.
RECEIPT_MOVED_OUT = (
    "UpdateReceiptRecord",
    "write_pipeline_receipt",
    "read_latest_pipeline_receipt",
    "acknowledge_pipeline_receipt",
    "read_latest_pipeline_receipt_dict",
    "latest_pipeline_receipt_summary",
    "record_verification",
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
    """``(added, deleted)`` for the fork's delta on ``path``, or None without a ref.

    A file the fork does not touch produces NO numstat line at all, so an
    unchanged file is ``(0, 0)`` -- not ``None``. ``None`` means only "there is
    no upstream ref to measure against", i.e. the ratchet cannot run; returning
    it for an unchanged file would make every zero-delta ratchet skip itself
    forever and never actually assert anything.
    """
    ref = next(
        (r for r in ("origin/main",) if _git("rev-parse", "--verify", "--quiet", r).returncode == 0),
        None,
    )
    if ref is None:
        return None
    base = _git("merge-base", "HEAD", ref).stdout.strip()
    if not base:
        return None
    # An unchanged file produces no numstat row -- and so does a path that does
    # not exist at all. Without this check a typo in the target path (or an
    # upstream rename) would make the ratchet assert 0 <= 0 and pass forever
    # while measuring nothing.
    if _git("cat-file", "-e", f"{base}:{path}").returncode != 0:
        raise AssertionError(
            f"{path} does not exist in the merge base {base}: the ratchet would measure "
            f"nothing and pass. Upstream renamed or removed the file, or the target path "
            f"is wrong -- point the ratchet at the file's new name."
        )
    result = _git("diff", "--numstat", base, "--", path)
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, changed = parts
        if changed == path and added.isdigit() and deleted.isdigit():
            return int(added), int(deleted)
    return (0, 0)


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


def test_update_receipt_fork_delta_stays_at_zero() -> None:
    """``update_receipt.py`` must stay byte-identical to upstream's.

    Upstream rewrites this file constantly; the fork's pipeline receipt contract lives in
    ``hermes_cli/update_receipt_fork.py``, so this file must carry no fork lines at all.
    """
    delta = _fork_delta(RECEIPT_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= RECEIPT_MAX_DELETED, (
        f"{RECEIPT_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {RECEIPT_MAX_DELETED}). Restore upstream's content and move the fork's "
        f"code into hermes_cli/update_receipt_fork.py."
    )
    assert added <= RECEIPT_MAX_ADDED, (
        f"{RECEIPT_TARGET}: the fork now adds {added} line(s) to upstream's file (budget "
        f"{RECEIPT_MAX_ADDED}). Even a pure insertion is a hunk upstream can collide with. "
        f"Move the fork's code into hermes_cli/update_receipt_fork.py."
    )


def test_moved_receipt_symbols_are_not_redefined_in_update_receipt() -> None:
    """The pipeline receipt contract must stay out of the upstream-owned file."""
    tree = ast.parse((REPO / RECEIPT_TARGET).read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for name in RECEIPT_MOVED_OUT:
        assert name not in defined, (
            f"{name} is defined inside {RECEIPT_TARGET} again. It belongs in "
            f"hermes_cli/update_receipt_fork.py: a definition in an upstream-owned file is a "
            f"merge-conflict magnet."
        )


def test_source_check_fork_delta_stays_at_zero() -> None:
    """``source_check.py`` must stay byte-identical to upstream's.

    The fork's update-check behavior (a local-only branch is a pin, not an error)
    lives in ``hermes_cli/source_check_fork.py``, which swaps ONE upstream symbol
    for the duration of a check. Upstream's own file must carry no fork lines:
    it is the file upstream keeps editing, so every fork line is a collision.
    """
    delta = _fork_delta(SOURCE_CHECK_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= SOURCE_CHECK_MAX_DELETED, (
        f"{SOURCE_CHECK_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {SOURCE_CHECK_MAX_DELETED}). Restore upstream's content and move the fork's "
        f"code into hermes_cli/source_check_fork.py."
    )
    assert added <= SOURCE_CHECK_MAX_ADDED, (
        f"{SOURCE_CHECK_TARGET}: the fork now adds {added} line(s) to upstream's file (budget "
        f"{SOURCE_CHECK_MAX_ADDED}). Even a pure insertion is a hunk upstream can collide with. "
        f"Move the fork's code into hermes_cli/source_check_fork.py."
    )


def test_source_check_test_file_fork_delta_stays_at_zero() -> None:
    """Upstream's own test file must stay byte-identical too.

    The fork's variants of upstream's assertions (a local-only branch is NOT an
    error) live in ``tests/hermes_cli/test_source_check_fork.py``; upstream's file
    must keep testing upstream's own behavior, byte for byte.
    """
    delta = _fork_delta(SOURCE_CHECK_TEST_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= SOURCE_CHECK_MAX_DELETED, (
        f"{SOURCE_CHECK_TEST_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {SOURCE_CHECK_MAX_DELETED}). Restore upstream's content and move the fork's "
        f"tests into tests/hermes_cli/test_source_check_fork.py."
    )
    assert added <= SOURCE_CHECK_MAX_ADDED, (
        f"{SOURCE_CHECK_TEST_TARGET}: the fork now adds {added} line(s) to upstream's file "
        f"(budget {SOURCE_CHECK_MAX_ADDED}). Move the fork's tests into "
        f"tests/hermes_cli/test_source_check_fork.py."
    )


def test_desktop_update_overlay_fork_delta_stays_at_zero() -> None:
    """The Desktop's update overlay must stay byte-identical to upstream's.

    Upstream keeps editing this file, so every fork line in it is a hunk a
    merge can collide with -- and a collision aborts ``hermes update`` with
    exit 1. The fork's receipt pane lives in
    ``apps/desktop/src/app/updates-overlay-fork.tsx``; this file must carry no
    fork lines at all.
    """
    delta = _fork_delta(DESKTOP_OVERLAY_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= DESKTOP_OVERLAY_MAX_DELETED, (
        f"{DESKTOP_OVERLAY_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {DESKTOP_OVERLAY_MAX_DELETED}). Restore upstream's content and move the fork's "
        f"code into apps/desktop/src/app/updates-overlay-fork.tsx."
    )
    assert added <= DESKTOP_OVERLAY_MAX_ADDED, (
        f"{DESKTOP_OVERLAY_TARGET}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {DESKTOP_OVERLAY_MAX_ADDED}). Even a pure insertion is a hunk upstream can "
        f"collide with. Move the fork's code into "
        f"apps/desktop/src/app/updates-overlay-fork.tsx."
    )


def test_desktop_wiring_fork_delta_stays_at_one_import() -> None:
    """The wiring may point at the fork's overlay -- but only that one import.

    Every other line in ``wiring.tsx`` must stay upstream's: the file is
    upstream's, and a fork line in it is a hunk upstream has to edit in the
    same place to collide.
    """
    delta = _fork_delta(DESKTOP_WIRING_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= DESKTOP_WIRING_MAX_DELETED, (
        f"{DESKTOP_WIRING_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {DESKTOP_WIRING_MAX_DELETED}). Only the overlay import may differ."
    )
    assert added <= DESKTOP_WIRING_MAX_ADDED, (
        f"{DESKTOP_WIRING_TARGET}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {DESKTOP_WIRING_MAX_ADDED}). Only the overlay import may differ -- move "
        f"anything else into a fork-owned module."
    )


def test_desktop_updates_store_fork_delta_stays_within_budget() -> None:
    """The Desktop's update store may keep its fork delta, but only this much.

    ``apps/desktop/src/store/updates.ts`` is upstream's file: the fork adds the
    two update-settings atoms and the post-apply relaunch fix to it. The receipt
    atoms were moved OUT to ``apps/desktop/src/store/update-receipt-fork.ts``
    (the fork-owned module the overlay imports), so this budget is the delta
    that is left -- it must not grow back.
    """
    delta = _fork_delta(DESKTOP_UPDATES_STORE_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= DESKTOP_UPDATES_STORE_MAX_DELETED, (
        f"{DESKTOP_UPDATES_STORE_TARGET}: the fork now DELETES {deleted} line(s) of upstream's "
        f"file (budget {DESKTOP_UPDATES_STORE_MAX_DELETED}). Every deleted line is a line "
        f"upstream has to restore in the same hunk to collide."
    )
    assert added <= DESKTOP_UPDATES_STORE_MAX_ADDED, (
        f"{DESKTOP_UPDATES_STORE_TARGET}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {DESKTOP_UPDATES_STORE_MAX_ADDED}). Move new code into a fork-owned module "
        f"(e.g. apps/desktop/src/store/update-receipt-fork.ts) and leave a call site behind."
    )


def test_run_adapters_fork_delta_stays_within_budget() -> None:
    """The cross-profile lock-conflict parking must stay out of the upstream mixin.

    ``gateway/run_adapters.py`` is upstream's adapter lifecycle mixin, and the
    fork's lock-conflict behaviour (auto-recovery on holder PID exit) lives in
    the fork-owned ``gateway/reconnect_lock_fork.py``. Two thin delegations stay
    in the upstream file -- a pure insertion, so a routine upstream edit next to
    them merges cleanly.
    """
    delta = _fork_delta(RUN_ADAPTERS_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= RUN_ADAPTERS_MAX_DELETED, (
        f"{RUN_ADAPTERS_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {RUN_ADAPTERS_MAX_DELETED}). Move the fork's code into "
        f"gateway/reconnect_lock_fork.py."
    )
    assert added <= RUN_ADAPTERS_MAX_ADDED, (
        f"{RUN_ADAPTERS_TARGET}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {RUN_ADAPTERS_MAX_ADDED}). Move the fork's lock-conflict code into "
        f"gateway/reconnect_lock_fork.py and leave a call site behind."
    )


def test_actions_router_fork_delta_stays_within_budget() -> None:
    """The update routes must stay out of the upstream-owned router.

    ``hermes_cli/web_routers/actions.py`` is upstream's file, and one of the files
    upstream rewrites most. The fork's routes live in the fork-owned
    ``hermes_cli/web_routers/actions_fork.py``, which SHADOWS upstream's by being
    mounted first -- so this file must carry no fork lines at all.
    """
    delta = _fork_delta(ACTIONS_ROUTER_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= ACTIONS_ROUTER_MAX_DELETED, (
        f"{ACTIONS_ROUTER_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {ACTIONS_ROUTER_MAX_DELETED}). Every deleted line is a line upstream has to "
        f"restore in the same hunk to collide. Move the fork's code into "
        f"hermes_cli/web_routers/actions_fork.py."
    )
    assert added <= ACTIONS_ROUTER_MAX_ADDED, (
        f"{ACTIONS_ROUTER_TARGET}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {ACTIONS_ROUTER_MAX_ADDED}). Move the fork's routes into "
        f"hermes_cli/web_routers/actions_fork.py and leave the upstream file untouched."
    )


def test_web_server_fork_delta_stays_within_budget() -> None:
    """The router mount must stay a pure insertion of the fork's own router.

    ``hermes_cli/web_server.py`` is upstream's file. The fork mounts its own
    router BEFORE upstream's (so the fork's routes win their shared paths) --
    that mount is the fork's only footprint here. Every added line is a hunk
    upstream has to edit in the same place to collide, so the budget is the
    measured delta: the mount cannot grow back.
    """
    delta = _fork_delta(WEB_SERVER_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= WEB_SERVER_MAX_DELETED, (
        f"{WEB_SERVER_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {WEB_SERVER_MAX_DELETED}). Every deleted line is a line upstream has to "
        f"restore in the same hunk to collide."
    )
    assert added <= WEB_SERVER_MAX_ADDED, (
        f"{WEB_SERVER_TARGET}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {WEB_SERVER_MAX_ADDED}). The fork's whole footprint here is the import "
        f"and the include of hermes_cli/web_routers/actions_fork.py -- anything more belongs "
        f"in that fork-owned module."
    )


def test_desktop_source_probe_fork_delta_stays_within_budget() -> None:
    """The bridge may choose the fork's check entry -- and nothing more.

    ``checkout-source.ts`` is upstream's file. The fork's check behavior lives
    in ``hermes_cli/source_check_fork.py``; the bridge only selects it, because
    upstream's entry would report a fork-local-only branch as an error the
    About pane renders as "couldn't reach the update server". Any further
    fork code here is a hunk upstream has to collide with.
    """
    delta = _fork_delta(DESKTOP_SOURCE_PROBE_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= DESKTOP_SOURCE_PROBE_MAX_DELETED, (
        f"{DESKTOP_SOURCE_PROBE_TARGET}: the fork now DELETES {deleted} line(s) of upstream's "
        f"file (budget {DESKTOP_SOURCE_PROBE_MAX_DELETED}). Every deleted line is a line "
        f"upstream has to restore in the same hunk to collide."
    )
    assert added <= DESKTOP_SOURCE_PROBE_MAX_ADDED, (
        f"{DESKTOP_SOURCE_PROBE_TARGET}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {DESKTOP_SOURCE_PROBE_MAX_ADDED}). Move new code into "
        f"hermes_cli/source_check_fork.py and leave the module selection behind."
    )


def test_venv_sync_fork_delta_stays_within_budget() -> None:
    """The launch path's fork delta must stay a pure insertion of ONE call site.

    ``hermes_cli/venv_sync.py`` is upstream's, and it is the file every launch
    reads. The fork's part is the Desktop-tail decision (fork-owned module,
    one call): the decision itself must never come back here, because a
    rewritten block in this file is a hunk upstream has to collide with -- and
    a collision aborts ``hermes update`` with exit 1.
    """
    delta = _fork_delta(VENV_SYNC_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= VENV_SYNC_MAX_DELETED, (
        f"{VENV_SYNC_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {VENV_SYNC_MAX_DELETED}). Every deleted line is a line upstream has to "
        f"restore in the same hunk to collide."
    )
    assert added <= VENV_SYNC_MAX_ADDED, (
        f"{VENV_SYNC_TARGET}: the fork now ADDS {added} line(s) to upstream's file (budget "
        f"{VENV_SYNC_MAX_ADDED}). Keep the decision in hermes_cli/update_desktop_tail.py "
        f"and leave the one call site behind."
    )


def test_acl_step_fork_delta_stays_within_budget() -> None:
    """The ACL step's fork delta must stay a pure insertion of ONE call site.

    ``hermes_cli/update_windows_acl.py`` is upstream's, and it is the step that decides
    whether the user keeps access to the Hermes home. The fork's part is the incremental
    repair (fork-owned module, one call): the decision itself must never come back here,
    because a rewritten block in this file is a hunk upstream has to collide with -- and a
    collision aborts ``hermes update`` with exit 1.
    """
    delta = _fork_delta(ACL_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= ACL_MAX_DELETED, (
        f"{ACL_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file (budget "
        f"{ACL_MAX_DELETED}). Every deleted line is a line upstream has to restore in the "
        f"same hunk to collide. Move the fork's change into a fork-owned module."
    )
    assert added <= ACL_MAX_ADDED, (
        f"{ACL_TARGET}: the fork now ADDS {added} line(s) to upstream's file (budget "
        f"{ACL_MAX_ADDED}). Keep the repair in hermes_cli/update_acl_fast.py and leave the "
        f"one call site behind."
    )


def test_update_cmd_windows_fork_delta_stays_within_budget() -> None:
    """The Windows relaunch must stay out of upstream's lifecycle module.

    ``hermes_cli/update_cmd_windows.py`` is upstream's, and the fork's
    Scheduled-Task relaunch plus its post-update gate live in the fork-owned
    ``hermes_cli/update_cmd_windows_fork.py``. Only the delegation stays here.
    """
    delta = _fork_delta(UPDATE_CMD_WINDOWS_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= UPDATE_CMD_WINDOWS_MAX_DELETED, (
        f"{UPDATE_CMD_WINDOWS_TARGET}: the fork now DELETES {deleted} line(s) of upstream's "
        f"file (budget {UPDATE_CMD_WINDOWS_MAX_DELETED}). Move the fork's change into "
        f"hermes_cli/update_cmd_windows_fork.py."
    )
    assert added <= UPDATE_CMD_WINDOWS_MAX_ADDED, (
        f"{UPDATE_CMD_WINDOWS_TARGET}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {UPDATE_CMD_WINDOWS_MAX_ADDED}). Keep the relaunch in "
        f"hermes_cli/update_cmd_windows_fork.py and leave the delegation behind."
    )


def test_gitignore_fork_delta_stays_within_budget() -> None:
    """``.gitignore`` must stay a pure insertion of the fork's own rule.

    Upstream owns the ignore rules for the build caches, the PM lockfile and the
    electron-builder scratch dirs. Deleting them makes untracked debris dirty
    the tree, and the parked-branch guard then refuses every update -- the
    failure this file's own history already produced once.
    """
    delta = _fork_delta(GITIGNORE_TARGET)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= GITIGNORE_MAX_DELETED, (
        f"{GITIGNORE_TARGET}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {GITIGNORE_MAX_DELETED}). Every deleted ignore rule lets untracked debris "
        f"dirty the tree, and the parked-branch guard then refuses the update."
    )
    assert added <= GITIGNORE_MAX_ADDED, (
        f"{GITIGNORE_TARGET}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {GITIGNORE_MAX_ADDED}). Upstream's patterns already cover the build caches; "
        f"only a genuinely fork-specific rule belongs here."
    )


# The migrated files and where their fork code belongs when the delta grows again.
_MIGRATED_DELTA_BUDGETS = (
    (BANNER_TARGET, BANNER_MAX_ADDED, BANNER_MAX_DELETED,
     "hermes_cli/source_check_fork.py"),
    (STARTUP_FAST_TARGET, STARTUP_FAST_MAX_ADDED, STARTUP_FAST_MAX_DELETED,
     "hermes_cli/source_check_fork.py"),
    (WEB_COMMON_TARGET, WEB_COMMON_MAX_ADDED, WEB_COMMON_MAX_DELETED,
     "a fork-owned module next to the router"),
    (STATUS_INLINE_SOURCE_TARGET, STATUS_INLINE_SOURCE_MAX_ADDED, STATUS_INLINE_SOURCE_MAX_DELETED,
     "a fork-owned module next to the gateway"),
    (GROUP_EXTERNAL_WRITES_TARGET, GROUP_EXTERNAL_WRITES_MAX_ADDED, GROUP_EXTERNAL_WRITES_MAX_DELETED,
     "apps/desktop/src/plugins/hermes-bots/routing.ts"),
)


@pytest.mark.parametrize(
    "target,max_added,max_deleted,belongs_in", _MIGRATED_DELTA_BUDGETS
)
def test_migrated_file_fork_delta_stays_within_budget(
    target: str, max_added: int, max_deleted: int, belongs_in: str
) -> None:
    """A migrated file must keep its tiny, measured delta.

    Every one of these files used to DELETE a line of upstream's file, which made it
    a conflict magnet: ``hermes update`` merges ``origin/main`` into the fork's branch
    and ABORTS on any conflict (exit 1 -- the Desktop's "Update failed (exit 1)"
    banner). The migration turned each one into a pure insertion, and this budget is
    the ratchet that keeps it there.
    """
    delta = _fork_delta(target)
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    added, deleted = delta
    assert deleted <= max_deleted, (
        f"{target}: the fork now DELETES {deleted} line(s) of upstream's file "
        f"(budget {max_deleted}). Every deleted line is a line upstream has to restore "
        f"in the same hunk to collide. Move the fork's change into {belongs_in}."
    )
    assert added <= max_added, (
        f"{target}: the fork now ADDS {added} line(s) to upstream's file "
        f"(budget {max_added}). Keep the fork's code in {belongs_in} and leave the "
        f"call site behind."
    )


def test_fork_modules_exist() -> None:
    """The fork-owned modules the call sites point at must be present."""
    for path in (
        "hermes_cli/update_cmd_fork.py",
        "hermes_cli/update_cmd_windows_fork.py",
        "tests/hermes_cli/test_update_cmd_windows_fork.py",
        "hermes_cli/update_transient_retry.py",
        "hermes_cli/update_git_timeout.py",
        "hermes_cli/update_receipt_fork.py",
        "hermes_cli/source_check_fork.py",
        "hermes_cli/update_desktop_tail.py",
        "hermes_cli/update_acl_fast.py",
        "hermes_cli/web_routers/actions_fork.py",
        "tests/hermes_cli/test_actions_fork_routes.py",
        "tests/hermes_cli/test_source_check_fork.py",
        "tests/hermes_cli/test_source_check_fork_call_sites.py",
        "tests/hermes_cli/test_update_desktop_tail.py",
        "tests/hermes_cli/test_update_acl_fast.py",
        "hermes_cli/tests/update/test_update_receipt_legacy.py",
        "apps/desktop/src/app/updates-overlay-fork.tsx",
        "apps/desktop/src/app/updates-overlay-fork.test.tsx",
        "apps/desktop/src/store/update-receipt-fork.ts",
        "apps/desktop/src/store/update-receipt-fork.test.ts",
        "apps/desktop/src/store/updates-fork.ts",
        "apps/desktop/src/store/updates-fork.test.ts",
        "gateway/reconnect_lock_fork.py",
        "tests/gateway/test_reconnect_lock_fork.py",
        "tests/gateway/test_status_inline_source_fork.py",
        "apps/desktop/electron/updater/checkout-source-fork.test.ts",
    ):
        assert (REPO / path).is_file(), f"{path} is missing: the fork's call sites point at it"
