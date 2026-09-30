"""The parked-branch guard must never hang on a promisor lazy-fetch.

``_assess_parked_branch_switch`` runs ``git cherry origin/<target>`` to decide
whether a checkout parked on another branch may be moved to the update target.
``git cherry`` computes patch-ids, so it reads trees/blobs. In a treeless
partial clone (``remote.origin.promisor=true`` + ``partialclonefilter=tree:0``)
those objects are stored on demand, and git spawns a lazy fetch from the
promisor remote — with no timeout of its own. On a slow or stalling remote that
single call blocks ``hermes update`` indefinitely: observed at 98 minutes with
zero output after preflight.

The guard therefore must (a) never trigger a lazy fetch, (b) fall back to an
ancestry verdict that only needs commit objects when patch-ids are unavailable
locally, and (c) treat a timed-out git call as a completed failure rather than
letting ``subprocess.run`` propagate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import update_cmd_git


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def _install_git_recorder(monkeypatch, cherry: subprocess.CompletedProcess):
    """Record every git argv the guard spawns; answer with canned results."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, cwd=None, check=False, **kw):
        argv = list(argv)
        calls.append((argv, kw))
        if "cherry" in argv:
            return _cp(argv, cherry.returncode, cherry.stdout, cherry.stderr)
        if "status" in argv:
            return _cp(argv, 0, "")
        if argv[1:2] == ["merge-base"]:
            # origin/main is NOT an ancestor of the parked branch
            return _cp(argv, 1)
        if argv[1:2] == ["rev-list"] and "--count" in argv:
            return _cp(argv, 0, "3\n")
        return _cp(argv, 0)

    monkeypatch.setattr(update_cmd_git.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def no_config(monkeypatch):
    """Keep ``load_config`` out of the test: the guard's verdict is what's under test."""
    import hermes_cli.config as config

    monkeypatch.setattr(config, "load_config", lambda *a, **k: {"updates": {}})
    return config


def test_cherry_never_lazy_fetches(monkeypatch, tmp_path: Path, no_config):
    """``git cherry`` must run with lazy fetch disabled and a bounded timeout."""
    cherry = _cp(["git", "cherry"], 128, "", "fatal: unable to read tree (abc123)")
    calls = _install_git_recorder(monkeypatch, cherry)

    update_cmd_git._assess_parked_branch_switch(["git"], tmp_path, "local-main-clean", "main")

    cherry_calls = [(a, kw) for a, kw in calls if "cherry" in a]
    assert cherry_calls, "the parked-branch guard must still ask git cherry"

    _, kw = cherry_calls[0]
    env = kw.get("env") or {}
    assert env.get("GIT_NO_LAZY_FETCH") == "1", (
        "git cherry must not spawn a promisor lazy-fetch: it hangs the update with no timeout"
    )
    assert kw.get("timeout") is not None, "a network-capable git call must be bounded"


def test_falls_back_to_ancestry_when_patch_ids_unavailable(
    monkeypatch, tmp_path: Path, no_config
):
    """A treeless clone can't compute patch-ids — the guard must still give a verdict."""
    cherry = _cp(["git", "cherry"], 128, "", "fatal: unable to read tree (abc123)")
    _install_git_recorder(monkeypatch, cherry)

    safe, reason = update_cmd_git._assess_parked_branch_switch(
        ["git"], tmp_path, "local-main-clean", "main"
    )

    assert safe is True, "an unreadable tree must not abort the update; commits-only checks still work"
    assert reason.startswith("unmerged:"), reason
    assert reason == "unmerged:3", reason


def test_unmerged_parks_still_reports_count(monkeypatch, tmp_path: Path, no_config):
    """Normal path unchanged: patch-ids available, some commits not upstream."""
    cherry = _cp(["git", "cherry"], 0, "+ aaaa\n- bbbb\n+ cccc\n")
    _install_git_recorder(monkeypatch, cherry)

    safe, reason = update_cmd_git._assess_parked_branch_switch(
        ["git"], tmp_path, "feature", "main"
    )

    assert (safe, reason) == (True, "unmerged:2")


def test_git_run_timeout_returns_completed_process(tmp_path: Path):
    """A timed-out git call must come back as a failure, not propagate or block."""
    def exploding_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=kw.get("timeout", 1))

    real = subprocess.run
    subprocess.run = exploding_run
    try:
        result = update_cmd_git._git_run(["git"], ["cherry", "origin/main"], tmp_path, timeout=5)
    finally:
        subprocess.run = real

    assert result.returncode == 124
    assert "timed out" in result.stderr
