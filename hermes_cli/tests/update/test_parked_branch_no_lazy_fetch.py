"""The parked-branch guard must never hang on a promisor lazy-fetch.

``_assess_parked_branch_switch`` decides whether a checkout parked on another
branch may be moved to the update target. Two of its git calls read trees:

* ``git cherry`` computes patch-ids, so it reads trees/blobs. In a treeless
  partial clone (``remote.origin.promisor=true`` + ``partialclonefilter=tree:0``)
  those objects live on the promisor remote, and git spawns a lazy fetch from it
  — with no timeout of its own. On a slow or stalling remote that single call
  blocks ``hermes update`` indefinitely: observed at 98 minutes with zero output
  after preflight, and again upstream as 332 packs / 180 GiB over 7 h (#131444).

The guard therefore must (a) count parked-only commits from the commit graph
first, which needs no trees, (b) never reach ``cherry`` at all on a partial
clone, and (c) treat a timed-out git call as a completed failure rather than
letting ``subprocess.run`` propagate.

Real-partial-clone coverage of (a) and (b) lives in
``tests/hermes_cli/test_update_parked_branch_guard.py``; this file pins the
argv-level contract and the timeout conversion.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import update_cmd_git

PROMISOR_RE = "^(remote\\..*\\.promisor|extensions\\.partialclone)$"


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def _install_git_recorder(
    monkeypatch,
    cherry: subprocess.CompletedProcess,
    ahead: str = "3\n",
    promisor: str = "",
) -> list[tuple[list[str], dict]]:
    """Record every git argv the guard spawns; answer with canned results."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, cwd=None, check=False, **kw):
        argv = list(argv)
        calls.append((argv, kw))
        if "cherry" in argv:
            return _cp(argv, cherry.returncode, cherry.stdout, cherry.stderr)
        if "status" in argv:
            return _cp(argv, 0, "")
        if "config" in argv:
            return _cp(argv, 0, promisor)
        if "rev-list" in argv and "--count" in argv:
            return _cp(argv, 0, ahead)
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
    """On a promisor/partial clone the guard must not reach cherry at all.

    ``GIT_NO_LAZY_FETCH`` is ignored by git before 2.44, so bounding the call is
    not enough — the only safe answer on a partial clone is the commit count.
    """
    cherry = _cp(["git", "cherry"], 128, "", "fatal: unable to read tree (abc123)")
    calls = _install_git_recorder(
        monkeypatch, cherry, promisor="remote.origin.promisor true\n"
    )

    safe, reason = update_cmd_git._assess_parked_branch_switch(
        ["git"], tmp_path, "local-main-clean", "main"
    )

    assert not [a for a, _ in calls if "cherry" in a], (
        "git cherry lazy-fetches on a partial clone and must never be reached there"
    )
    assert (safe, reason) == (True, "unmerged:3")


def test_falls_back_to_ancestry_when_patch_ids_unavailable(
    monkeypatch, tmp_path: Path, no_config
):
    """A full clone whose cherry fails must still give a verdict, not "unverifiable"."""
    cherry = _cp(["git", "cherry"], 128, "", "fatal: unable to read tree (abc123)")
    calls = _install_git_recorder(monkeypatch, cherry, promisor="")

    safe, reason = update_cmd_git._assess_parked_branch_switch(
        ["git"], tmp_path, "local-main-clean", "main"
    )

    assert safe is True, "an unreadable tree must not abort the update; commits-only checks still work"
    assert reason == "unmerged:3", reason
    _, kw = [pair for pair in calls if "cherry" in pair[0]][0]
    assert (kw.get("env") or {}).get("GIT_NO_LAZY_FETCH") == "1", (
        "even off a partial clone, cherry must not be allowed to fetch"
    )
    assert kw.get("timeout") is not None, "a network-capable git call must be bounded"


def test_unmerged_parks_still_reports_count(monkeypatch, tmp_path: Path, no_config):
    """Normal path unchanged: patch-ids available, some commits not upstream."""
    cherry = _cp(["git", "cherry"], 0, "+ aaaa\n- bbbb\n+ cccc\n")
    _install_git_recorder(monkeypatch, cherry, promisor="")

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
