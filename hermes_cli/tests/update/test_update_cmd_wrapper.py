"""Tests for the ``--use-orchestrator`` wrapper in ``update_cmd``.

The wrapper (``_cmd_update_via_orchestrator``) is the thin shim that
``cmd_update`` invokes behind the ``--use-orchestrator`` flag. It translates
the new ``UpdateOutcome`` dataclass into a CLI exit code, the same contract
the legacy path produces. These tests pin that contract.

Most tests monkeypatch ``hermes_cli.update_orchestrator.run_update`` so they
do not depend on git state. One end-to-end test runs the real orchestrator
through the wrapper against the ``update_world`` fixture to confirm the
wrapper honours the orchestrator's exit code on a real failure mode
(detached HEAD → aborted).
"""
from __future__ import annotations

import os
import subprocess
import types
from pathlib import Path

import pytest

from hermes_cli import update_cmd
from hermes_cli.update_orchestrator import UpdateOutcome


# ---------------------------------------------------------------------------
# Mocks / helpers
# ---------------------------------------------------------------------------


class _StubMainModule(types.SimpleNamespace):
    """Stand-in for ``hermes_cli.main`` exposing only the attributes the
    wrapper reads (``PROJECT_ROOT``).

    Returning this from a monkeypatched ``_m()`` keeps the wrapper off the
    real ``hermes_cli.main`` import chain in unit tests.
    """


@pytest.fixture
def stub_main(monkeypatch, hermetic_git_repo):
    """Patch ``_m`` so ``_m().PROJECT_ROOT`` returns the hermetic repo.

    Also patches the local ``get_hermes_home`` reference in ``update_cmd`` to
    point at the fixture's home. ``PROJECT_ROOT`` is used as the repo argument
    and ``get_hermes_home()`` as the home argument to ``run_update``.
    """
    fake_main = _StubMainModule(PROJECT_ROOT=hermetic_git_repo.repo)
    monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)
    monkeypatch.setattr(
        update_cmd, "get_hermes_home",
        lambda: Path(os.environ["HERMES_HOME"]),
    )
    return fake_main


def _patch_run_update(monkeypatch, outcome: UpdateOutcome):
    """Replace ``hermes_cli.update_orchestrator.run_update`` with a stub.

    The wrapper imports ``run_update`` lazily inside the function body
    (``from hermes_cli.update_orchestrator import run_update as _run_update``),
    so patching the module attribute here is sufficient: each call resolves
    the name at invocation time.
    """
    monkeypatch.setattr(
        "hermes_cli.update_orchestrator.run_update",
        lambda **_: outcome,
    )


def _args(**overrides) -> types.SimpleNamespace:
    """Construct a minimal argparse-like namespace for the wrapper."""
    base = {
        "force": False,
        "auto_apply_safe": False,
        "use_orchestrator": True,
        "gateway": False,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Contract tests (mocked orchestrator)
# ---------------------------------------------------------------------------


def test_wrapper_exists_and_is_callable(stub_main):
    """The wrapper is importable and has the expected signature."""
    assert callable(update_cmd._cmd_update_via_orchestrator)


def test_wrapper_returns_zero_on_success(stub_main, monkeypatch):
    """A success outcome from the orchestrator maps to exit code 0."""
    _patch_run_update(
        monkeypatch,
        UpdateOutcome(outcome="success", receipt_id="abc12345", steps=[]),
    )
    args = _args()
    code = update_cmd._cmd_update_via_orchestrator(args, gateway_mode=False)
    assert code == 0


def test_wrapper_returns_one_on_conflict(stub_main, monkeypatch):
    """A conflict outcome (MERGE_HEAD preserved) maps to exit code 1.

    This is the regression the wrapper exists to enforce: the orchestrator
    defaults ``exit_code`` to 0 for every non-success outcome, but the CLI
    contract is "non-zero on user-actionable failure". The wrapper does the
    translation so a merge conflict (left in MERGE_HEAD per spec) surfaces as
    a failure to the calling shell.
    """
    _patch_run_update(
        monkeypatch,
        UpdateOutcome(
            outcome="conflict",
            error="merge-conflict",
            receipt_id="conf0001",
            steps=[{
                "name": "merge", "ok": False,
                "detail": ["apps/desktop/src/main.tsx", "hermes_cli/main.py"],
            }],
        ),
    )
    args = _args()
    code = update_cmd._cmd_update_via_orchestrator(args, gateway_mode=False)
    assert code == 1


def test_wrapper_returns_one_on_aborted(stub_main, monkeypatch):
    """An aborted outcome (e.g., detached HEAD, dirty tree) maps to exit 1."""
    _patch_run_update(
        monkeypatch,
        UpdateOutcome(outcome="aborted", error="detached-head", steps=[]),
    )
    args = _args()
    code = update_cmd._cmd_update_via_orchestrator(args, gateway_mode=False)
    assert code == 1


def test_wrapper_honours_explicit_exit_code(stub_main, monkeypatch):
    """When the orchestrator sets ``exit_code`` explicitly, the wrapper returns it.

    The receipt-write-failed success branch is the only place the
    orchestrator currently overrides the default; this test pins that
    contract for future branches.
    """
    _patch_run_update(
        monkeypatch,
        UpdateOutcome(
            outcome="success",
            receipt_id="",
            steps=[],
            exit_code=7,
        ),
    )
    args = _args()
    code = update_cmd._cmd_update_via_orchestrator(args, gateway_mode=False)
    assert code == 7


def test_wrapper_translates_force_flag(stub_main, monkeypatch):
    """``args.force`` is forwarded to ``run_update(force=True)``."""
    seen = {}

    def fake_run_update(*, repo, home, force=False, auto_apply_safe=False, **kwargs):
        seen["force"] = force
        seen["auto_apply_safe"] = auto_apply_safe
        seen["repo"] = repo
        seen["home"] = home
        return UpdateOutcome(outcome="no-op", error="install-method-git")

    monkeypatch.setattr("hermes_cli.update_orchestrator.run_update", fake_run_update)

    args = _args(force=True, auto_apply_safe=True)
    update_cmd._cmd_update_via_orchestrator(args, gateway_mode=False)

    assert seen["force"] is True
    assert seen["auto_apply_safe"] is True


# ---------------------------------------------------------------------------
# End-to-end test (real orchestrator)
# ---------------------------------------------------------------------------


def test_wrapper_real_detached_head_returns_one(stub_main, hermetic_git_repo):
    """Run the real orchestrator through the wrapper on a detached HEAD.

    The hermetic repo is moved into detached HEAD, then the wrapper is
    invoked. The orchestrator should abort (preflight) with
    ``detached-head`` and the wrapper should return exit code 1. This is the
    only failure-mode end-to-end test we need because the orchestrator's 21
    failure-mode tests already exercise each terminal branch on its own; this
    one just confirms the wrapper's translation layer does not swallow the
    real signal.
    """
    subprocess.check_call(
        ["git", "-C", str(hermetic_git_repo.repo), "checkout", "--detach", "HEAD"]
    )
    args = _args()
    code = update_cmd._cmd_update_via_orchestrator(args, gateway_mode=False)
    assert code == 1
