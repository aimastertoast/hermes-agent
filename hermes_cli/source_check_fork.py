"""Fork-owned update-check behavior for a fork install.

Upstream's ``hermes_cli/source_check.py`` answers a branch the remote never
advertised with ``error="branch-local-only"`` and no target. The Desktop renders
ANY error as "couldn't reach the update server", which is a lie for a fork
install: the server is reachable, and ``hermes update`` merges ``origin/main``
into the checked-out branch IN PLACE, so "behind" has to be measured against
main.

Upstream's file must stay byte-identical to upstream (every fork line in it is a
hunk a merge can collide with), so the fork's behavior lives here:

* ``_branch_tip``, ``_behind_count`` and ``_check_branch`` are FORK COPIES of
  upstream's, carrying the fork's three changes: a 422 answer from GitHub means
  the ref is gone, a local ancestry count answers a fork-local HEAD that the
  compare API 404s, and a local-only branch is measured against main instead of
  reported as an error.
* ``check_for_updates`` runs upstream's own orchestration with ONE symbol --
  ``_check_branch`` -- swapped for the duration of the call, so upstream's
  caching, channel resolution and release handling keep evolving with upstream.

The copies are the price of a zero-delta upstream file: when upstream changes
them, the fork keeps its behavior until the copy is re-synced. The fork's own
tests (``tests/hermes_cli/test_source_check_fork.py``) pin that behavior.

Re-syncing a copy: take upstream's text, re-apply the fork's change, and call
upstream's helpers THROUGH the module (``source_check._git_run``) rather than
importing them -- an imported name is frozen at import time, and a test that
monkeypatches ``source_check._git_run`` must still reach the fork.
"""
from __future__ import annotations

import threading
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from hermes_cli import source_check

#: How many commits the local count may report. The list is a presentation of
#: the count, not the count itself.
_LOCAL_COMMIT_LIMIT = 250
#: The one bounded fetch the local count may spend when the target is unknown here.
_LOCAL_FETCH_TIMEOUT_S = 30

#: The swap below is process-wide for the duration of one check, so concurrent
#: checks must not restore the symbol under each other. A reference count, not
#: a lock held across the check: a check can block for tens of seconds (network
#: plus git), and a lock held that long would make every later check wait
#: behind a hung one. The lock below only guards the counter itself.
_CHECK_LOCK = threading.Lock()
_CHECK_DEPTH = 0
#: Upstream's own ``_check_branch``, captured when the swap is installed.
_CHECK_ORIGINAL: Optional[object] = None


def _branch_tip(repository: str | None, branch: str, root: Path, git: str,
                remote: str = "origin") -> tuple[str | None, bool, str | None]:
    """``(sha, missing, failure)``: ``missing`` only on a confirmed empty advertisement;
    ``failure`` names why no tip could be read, for the user-facing message."""
    # A successful empty ref advertisement alone proves a branch was deleted.
    # GitHub 404 can also mean a private repository: it must not heal a branch.
    failure = None
    absent = False
    if repository:
        from hermes_cli.github_api import describe_github_failure, github_token
        try:
            sha = source_check._request(
                f"https://api.github.com/repos/{repository}/commits/{quote(branch, safe='')}",
                "application/vnd.github.sha")
        except Exception as exc:
            sha = None
            failure = describe_github_failure(exc, authenticated=github_token() is not None)
            # 422 ("No commit found for SHA: <ref>") is only sent once GitHub has
            # resolved the repository, so unlike 404 -- which a private repository
            # also returns -- it does mean this ref is gone. Trusting it keeps a
            # slow link from undoing the answer: when `ls-remote` times out it
            # reports the branch as still present, and every caller then reads
            # a long-deleted branch as reachable.
            absent = isinstance(exc, urllib.error.HTTPError) and exc.code == 422
        if source_check._is_full_sha(sha):
            return sha, False, None
        if failure is None:
            failure = "api.github.com returned no commit for the branch."
        if absent:
            return None, True, None
        if branch == "main" and remote == "origin":
            return None, False, failure
    result = source_check._git_run(["ls-remote", "--exit-code", "--heads", remote, f"refs/heads/{branch}"],
                                   cwd=root, git=git, timeout=10)
    if result is None:
        return None, False, failure or f"`git ls-remote {remote}` could not run."
    sha = result.stdout.split()[0] if result.returncode == 0 and result.stdout else None
    if source_check._is_full_sha(sha):
        return sha, False, None
    if result.returncode == 2:
        return None, True, None
    detail = (result.stderr or "").strip().splitlines()
    return None, False, failure or (f"`git ls-remote {remote}` failed: {detail[-1]}" if detail
                                    else f"`git ls-remote {remote}` returned no tip.")


def _rev_list_count(co, rev_range: str) -> Optional[int]:
    """``git rev-list --count`` over ``rev_range``, run with the INSTALLED git.

    Upstream's ``_git_count`` takes no ``git`` argument, so it runs the bare
    ``git`` from PATH. Every other probe in this module passes ``git=co.git``
    -- the copy upstream resolves for an install without system Git -- so this
    one probe was the only one that failed there, and it failed the WHOLE local
    count: the behind-count fell back to the unknown sentinel on exactly the
    installs the fork targets.
    """
    result = source_check._git_run(["rev-list", "--count", rev_range],
                                    cwd=co.root, git=co.git, timeout=10)
    if result is None or result.returncode != 0:
        return None
    return source_check._quiet(lambda: int(result.stdout.strip()), None)


def _local_behind(co, target: str, *,
                  fetch_from: Optional[tuple[str, str]] = None) -> Optional[tuple[int, list[dict]]]:
    """``(behind, commits)`` counted from local objects, or None when ``target`` is unknown here.

    A fork-local HEAD breaks the GitHub compare: the base SHA only exists in the
    fork, so api.github.com answers 404. But this checkout still carries the
    upstream history its updater fetched, and ancestry answers locally without
    touching the network. ``fetch_from=(remote, branch)`` permits exactly one
    bounded fetch when even the tip is missing, for the window between an
    upstream push and the checkout's next update.
    """
    if co.embedded:
        return None
    commit = f"{target}^{{commit}}"
    if not source_check._git_ok(["cat-file", "-e", commit], cwd=co.root, git=co.git):
        if fetch_from is None or not source_check._git_ok(
                ["fetch", "--no-tags", "--quiet", fetch_from[0], fetch_from[1]],
                cwd=co.root, git=co.git, timeout=_LOCAL_FETCH_TIMEOUT_S):
            return None
        if not source_check._git_ok(["cat-file", "-e", commit], cwd=co.root, git=co.git):
            return None
    count = _rev_list_count(co, f"{co.head}..{target}")
    if count is None:
        return None
    if count == 0:
        return 0, []
    rows = []
    result = source_check._git_run(
        ["log", f"--max-count={_LOCAL_COMMIT_LIMIT}", "--format=%H%x1f%ct%x1f%an%x1f%s",
         f"{co.head}..{target}"], cwd=co.root, git=co.git, timeout=10)
    if result is not None and result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.split("\x1f", 3)
            if len(parts) != 4:
                continue
            sha, at, author, summary = parts
            # ``%ct`` is the COMMITTER date, like upstream's compare-API path
            # (``commit.committer.date``); the author date would disagree with it.
            rows.append({"sha": sha, "summary": summary, "author": author,
                         "at": source_check._quiet(lambda: int(at) * 1000, 0)})
    return count, rows[::-1]


def _behind_count(co, target: str, *,
                  fetch_from: Optional[tuple[str, str]] = None) -> tuple[int, list[dict]]:
    """``(behind, commits)`` for ``target``: local ancestry, then local objects, then the GitHub compare API."""
    if co.head == target or (not co.embedded and source_check._git_ok(
            ["merge-base", "--is-ancestor", target, co.head], cwd=co.root, git=co.git)):
        return 0, []
    # Order matters: the bounded fetch is last resort, spent only when neither
    # local objects nor the compare API can answer. A worktree checking a
    # remote-only tip must reach the compare, not attempt a ref-writing fetch.
    local = _local_behind(co, target)
    if local is not None:
        return local
    if co.repository:
        payload = source_check._github_compare(co.head, target, co.repository)
        ahead = (payload or {}).get("ahead_by")
        if isinstance(ahead, int) and not isinstance(ahead, bool) and ahead >= 0:
            return ahead, (source_check._quiet(lambda: source_check._commits(payload), []) if ahead else [])
    local = _local_behind(co, target, fetch_from=fetch_from)
    if local is not None:
        return local
    return source_check.UPDATE_AVAILABLE_NO_COUNT, []


def _check_branch(result: dict, co, selected_branch: str, *,
                  heal: Optional[tuple[Path, dict]]) -> None:
    """Compare the checkout with ``selected_branch``'s remote tip, falling back to main if it was deleted."""
    result["branch"] = selected_branch
    remote = source_check._branch_remote(co, selected_branch)
    target, missing, failure = _branch_tip(co.repository, selected_branch, co.root, co.git, remote)
    target_branch = selected_branch
    reason = (source_check._unhealable_reason(co, selected_branch)
              if missing and selected_branch != "main" else None)
    if reason:
        # The pin stays -- never heal away work the remote never had -- but the
        # comparison target becomes main: `hermes update` merges origin/main into
        # this branch in place, so that is what "behind" has to mean. Returning an
        # error here made the Desktop render "couldn't reach the update server"
        # (it maps every error to that line) on every check of a fork install.
        detail = ("has never been pushed" if reason == "never-pushed"
                  else "is gone from the remote but has commits that are not in main")
        result.update(localOnly=True,
                      message=f"Branch '{selected_branch}' {detail}; keeping it instead of switching to main.")
        target, _, failure = _branch_tip(co.repository, "main", co.root, co.git,
                                          remote if co.embedded else "origin")
        target_branch = "main"
    elif missing and selected_branch != "main":
        result["branch"] = "main"
        if heal:
            source_check._heal_deleted_branch(*heal)
        target, _, failure = _branch_tip(co.repository, "main", co.root, co.git,
                                          remote if co.embedded else "origin")
        target_branch = "main"
    if target is None:
        result.update(error="fetch-failed",
                      message=f"Could not resolve the remote branch tip: {failure}" if failure
                      else "Could not resolve the remote branch tip.")
        return
    # The fetch fallback must fetch the branch the target came from -- after the
    # localOnly fallback that is main, never the unpinned branch itself.
    behind, commits = _behind_count(co, target, fetch_from=(remote, target_branch))
    result["commits"] = commits
    result.update(targetSha=target, behind=behind, updateAvailable=behind != 0)


@contextmanager
def _fork_branch_check():
    """Run one check with the fork's branch check in place of upstream's.

    The swap is scoped to the call and guarded by a reference count: upstream's
    ``check_for_updates`` looks ``_check_branch`` up in its own module, so this
    is the only seam that reaches the fork's behavior without editing
    upstream's file. Concurrent checks share one swap -- the last one out
    restores upstream's symbol -- and a check that hangs holds nothing but the
    counter, so a later check is never blocked behind it.

    The swap is process-wide, not thread-local: for the duration of a check a
    direct ``source_check.check_for_updates`` from another thread would also
    get the fork's branch check. In this repo nothing calls it directly but
    this module (the three production call sites and the Desktop bridge, which
    runs this file's CLI entry, point here), and the fork's check differs from
    upstream's only in how a local-only branch is answered.
    """
    global _CHECK_DEPTH, _CHECK_ORIGINAL
    with _CHECK_LOCK:
        if _CHECK_DEPTH == 0:
            _CHECK_ORIGINAL = source_check._check_branch
            source_check._check_branch = _check_branch
        _CHECK_DEPTH += 1
    try:
        yield
    finally:
        with _CHECK_LOCK:
            _CHECK_DEPTH -= 1
            if _CHECK_DEPTH == 0 and _CHECK_ORIGINAL is not None:
                source_check._check_branch = _CHECK_ORIGINAL
                _CHECK_ORIGINAL = None


def check_for_updates(**kwargs) -> dict:
    """Upstream's update check, with the fork's local-only-branch behavior.

    Every argument is upstream's own (``install_root``, ``home``, ``branch``,
    ``channel``, ``cache_path``, ``branch_config_path``, ``force``,
    ``passive``, ``git``); they are passed straight through, so an argument
    upstream adds later works here without a change.
    """
    with _fork_branch_check():
        return source_check.check_for_updates(**kwargs)


def main() -> None:
    """The CLI entry the Desktop bridge runs (``--run-module hermes_cli.source_check_fork``).

    The bridge used to run upstream's module, which bypassed this file and
    reported a fork-local-only branch as ``error="branch-local-only"`` -- the
    Desktop's red "couldn't reach the update server". Argument parsing, the
    ``--set-channel`` handling and the JSON on stdout stay upstream's own; only
    the branch check is swapped, and only for this process's one check.
    """
    with _fork_branch_check():
        source_check.main()


if __name__ == "__main__":
    main()
