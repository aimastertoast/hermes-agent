"""The fork's bound on the update path's LOCAL git calls — FORK-OWNED.

``git status --porcelain``/``cherry``/``rev-list --count``/``config --get-regexp`` are local
commands, but on a treeless partial clone they read trees that git fetches from the promisor
remote — with no timeout of its own. The update then sits there with no output (98 minutes
observed; upstream: 332 packs / 180 GiB over 7 h, #131444).

The bound lives in ``hermes_cli/update_git_timeout.py`` (fork-owned) and is installed into
``hermes_cli.update_cmd_git`` at import time, so upstream's ``_git_run`` stays byte-identical.
These tests pin the contract: the bound applies to the local commands only, an explicit
timeout wins over the default, and a timed-out git comes back as a FAILED RUN (exit 124)
rather than an exception.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import update_cmd_git
from hermes_cli.update_git_timeout import (
    LOCAL_GIT_TIMEOUT_SECONDS,
    _default_timeout_for,
)


def _record(monkeypatch, *, raise_timeout: bool = False) -> list[tuple[list[str], dict]]:
    """Record every git spawn; optionally make it time out like a stalled lazy-fetch."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, cwd=None, check=False, **kw):
        argv = list(argv)
        calls.append((argv, kw))
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout", 1))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd_git.subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize(
    "args",
    [
        ["status", "--porcelain"],
        ["rev-list", "--count", "origin/main..HEAD"],
        ["config", "--get-regexp", "^(remote\\..*\\.promisor|extensions\\.partialclone)$"],
        ["cherry", "origin/main"],
    ],
)
def test_local_commands_carry_the_default_bound(args: list[str]) -> None:
    """The commands that can trigger a promisor lazy-fetch are the bounded ones."""
    assert _default_timeout_for(args) == LOCAL_GIT_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "args",
    [["fetch", "origin", "main"], ["config", "--get", "http.sslCAInfo"], ["rev-parse", "HEAD"]],
)
def test_other_commands_stay_unbounded(args: list[str]) -> None:
    """A network or unrelated command keeps upstream's own (unbounded) behaviour."""
    assert _default_timeout_for(args) is None


def test_bounded_call_reaches_the_spawn_with_a_timeout(monkeypatch, tmp_path: Path) -> None:
    """The bound must reach the spawn itself — that is what stops the hang."""
    calls = _record(monkeypatch)
    update_cmd_git._git_run(["git"], ["status", "--porcelain"], tmp_path)
    assert len(calls) == 1
    assert calls[0][1].get("timeout") == LOCAL_GIT_TIMEOUT_SECONDS


def test_unbounded_call_reaches_the_spawn_without_a_timeout(monkeypatch, tmp_path: Path) -> None:
    """Nothing else changes: a fetch keeps upstream's behaviour."""
    calls = _record(monkeypatch)
    update_cmd_git._git_run(["git"], ["fetch", "origin", "main"], tmp_path)
    assert len(calls) == 1
    assert "timeout" not in calls[0][1]


def test_an_explicit_timeout_wins_over_the_default(monkeypatch, tmp_path: Path) -> None:
    """A caller that asks for its own bound gets it."""
    calls = _record(monkeypatch)
    update_cmd_git._git_run(["git"], ["cherry", "origin/main"], tmp_path, timeout=5)
    assert calls[0][1].get("timeout") == 5


def test_a_timed_out_git_comes_back_as_a_failed_run(monkeypatch, tmp_path: Path) -> None:
    """A timeout is a failed run (exit 124), not an exception: one error path for callers."""
    _record(monkeypatch, raise_timeout=True)
    result = update_cmd_git._git_run(["git"], ["cherry", "origin/main"], tmp_path, timeout=5)
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_a_timed_out_git_raises_when_the_caller_asked_for_check(monkeypatch, tmp_path: Path) -> None:
    """``check=True`` keeps its contract: a non-zero exit raises."""
    _record(monkeypatch, raise_timeout=True)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        update_cmd_git._git_run(["git"], ["cherry", "origin/main"], tmp_path, timeout=5, check=True)
    assert excinfo.value.returncode == 124
