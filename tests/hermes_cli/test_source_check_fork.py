"""The fork's update check: a local-only branch is a pin, not a failure.

Upstream's ``hermes_cli/source_check.py`` answers a branch the remote never
advertised with ``error="branch-local-only"``, which the Desktop renders as
"couldn't reach the update server". For a fork install that is a lie: the
server is reachable, and ``hermes update`` merges ``origin/main`` into the
checked-out branch IN PLACE.

Upstream's file must carry no fork lines (every one of them is a hunk a merge
can collide with), so the fork's behavior lives in
``hermes_cli/source_check_fork.py`` and the tests that pin it live HERE --
upstream's own test file keeps testing upstream's own behavior.

The fixture and its helpers are imported from upstream's test module: the
checkout, the bare origin and the commit helper are upstream's, and copying
them would let the fork's copy drift from the one upstream actually runs.
"""
import json
import subprocess
import urllib.error

import pytest

from tests.hermes_cli.test_source_check import _bare_origin, _commit_on, installation

# The fixture serves channel records over loopback; the reader's real HTTP path is the subject.
pytestmark = pytest.mark.real_release_channels


@pytest.mark.parametrize("pinned_by", ["desktop", "current"])
def test_fork_never_pushed_branch_keeps_its_pin(installation, pinned_by):
    """#105042: an empty advertisement for a branch that was never pushed is not a deletion.

    The fork's variant of upstream's test: the pin stays, and the branch is
    measured against main -- the branch ``hermes update`` merges -- instead of
    being reported as an error the Desktop renders as "couldn't reach the
    update server".
    """
    from hermes_cli.source_check_fork import check_for_updates
    root, _linked, home, _base, _head, _responses, requests, git = installation
    _bare_origin(installation)
    git("branch", "local-work")
    _commit_on(git, "local-work", "unpushed work")
    branch_file = home / "desktop-update.json"
    if pinned_by == "desktop":
        branch_file.write_text(json.dumps({"branch": "local-work"}))
    else:
        git("checkout", "-q", "local-work")
    status = check_for_updates(install_root=root, home=home, branch_config_path=branch_file)
    assert status["branch"] == "local-work", status
    # A local-only branch is a pin, not a failure: the Desktop renders any
    # ``error`` as "couldn't reach the update server", which is a lie here.
    assert "error" not in status, status
    assert status["localOnly"] is True
    assert "never been pushed" in status["message"]
    # It is still measured against the branch the updater actually merges.
    assert status["targetSha"] == git("rev-parse", "origin/main")
    assert status["behind"] == 0
    assert status["updateAvailable"] is False
    if pinned_by == "desktop":
        assert json.loads(branch_file.read_text()) == {"branch": "local-work"}
    else:
        assert not branch_file.exists()
    assert requests == []


def test_local_only_branch_behind_main_offers_the_update_instead_of_failing(installation):
    """#128xxx: the desktop showed a red "can't reach the update server" on a fork install.

    ``hermes update`` merges ``origin/main`` into the checked-out branch in place,
    so the behind-count must be measured against main even when the local branch
    has no remote counterpart -- otherwise every check is an error and the user
    can never see or act on a real update.
    """
    from hermes_cli.source_check_fork import check_for_updates
    root, _linked, home, _base, _head, _responses, _requests, git = installation
    _bare_origin(installation)
    git("checkout", "-q", "-b", "local-work")
    git("commit", "-q", "--allow-empty", "-m", "fork work")
    # main moves on by two commits after the local branch was cut.
    git("checkout", "-q", "main")
    git("commit", "-q", "--allow-empty", "-m", "upstream one")
    git("commit", "-q", "--allow-empty", "-m", "upstream two")
    git("push", "-q", "origin", "main")
    git("fetch", "-q", "origin")
    git("checkout", "-q", "local-work")

    status = check_for_updates(install_root=root, home=home, branch_config_path=home / "desktop-update.json")

    assert "error" not in status, status
    assert status["localOnly"] is True
    assert status["branch"] == "local-work"
    # behind is the real "how far behind main" signal: either a count, or the
    # unknown-count sentinel when no repository can supply one. Both mean "behind".
    assert status["behind"] != 0
    assert status["updateAvailable"] is True
    assert status["targetSha"] == git("rev-parse", "origin/main")


def test_apis_negative_answer_survives_a_slow_ls_remote(installation, monkeypatch):
    """A branch GitHub says is gone must not depend on ``ls-remote`` still answering.

    ``_branch_tip`` only trusted an empty ref advertisement for "this branch was
    deleted", so on a slow link the ten-second ``ls-remote`` timeout reported the
    branch as still present. The checker then believed a branch that no longer
    exists was reachable, and every check of a fork install came back as an error
    the Desktop renders as "couldn't reach the update server".
    """
    from hermes_cli import source_check
    from hermes_cli.source_check_fork import check_for_updates

    root, _linked, home, _base, _head, _responses, _requests, git = installation
    _bare_origin(installation)
    git("branch", "local-work")
    git("checkout", "-q", "local-work")
    (root / "fork.txt").write_text("fork work\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "fork work")
    work = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    # main moves on by two commits after the local branch was cut. They touch real
    # files: `git cherry` matches on patch-id and every empty commit shares one, so
    # empty upstream commits would read as the fork work already being merged.
    (root / "upstream-one.txt").write_text("one\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "upstream one")
    (root / "upstream-two.txt").write_text("two\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "upstream two")
    git("push", "-q", "origin", "main")
    git("fetch", "-q", "origin")
    # The behind-count is measured from HEAD, so sit on the pinned branch.
    git("checkout", "-q", "local-work")
    # Keep the GitHub URL so the checker really does take the api.github.com path
    # (a local path yields no repository, and `_branch_tip` would skip straight
    # to `ls-remote`, which is the very thing this test takes away).
    git("remote", "set-url", "origin", "https://github.com/fixture/fork.git")
    # Published upstream once, then deleted there: the upstream config survives a
    # prune exactly as it does on the live install.
    git("config", "branch.local-work.merge", "refs/heads/local-work")
    git("config", "branch.local-work.remote", "origin")
    branch_file = home / "desktop-update.json"
    branch_file.write_text(json.dumps({"branch": "local-work"}))

    main_tip = git("rev-parse", "origin/main")

    def request(url, accept="application/vnd.github+json"):
        if url.endswith("/commits/main"):
            return main_tip
        if url.endswith("/commits/local-work"):
            raise urllib.error.HTTPError(url, 422, "Unprocessable Entity", {}, None)
        return json.dumps({"ahead_by": 2, "commits": []})

    real_git_run = source_check._git_run

    def slow_ls_remote(args, **kw):
        if args and args[0] == "ls-remote":
            return None  # the ten-second timeout, which carries no answer at all
        return real_git_run(args, **kw)

    monkeypatch.setattr(source_check, "_request", request)
    monkeypatch.setattr(source_check, "_git_run", slow_ls_remote)

    status = check_for_updates(install_root=root, home=home, branch_config_path=branch_file)

    assert "error" not in status, status
    assert status.get("localOnly") is True, status
    assert status["branch"] == "local-work"
    assert json.loads(branch_file.read_text())["branch"] == "local-work"
    # Measured against main, which is the branch `hermes update` merges.
    assert status["targetSha"] == main_tip
    assert status["behind"] == 2
    assert status["updateAvailable"] is True
    assert work  # the pin protects this commit


@pytest.mark.parametrize("merge", ["fast-forward", "rebase", "unmerged"])
def test_fork_deleted_remote_branch_keeps_its_pin_when_unmerged(installation, merge):
    """The fork's variant of upstream's healing test: an unmerged branch keeps its pin."""
    from hermes_cli.source_check_fork import check_for_updates
    root, _linked, home, _base, _head, _responses, _requests, git = installation
    _bare_origin(installation)
    git("branch", "pushed")
    work = _commit_on(git, "pushed", "published work")
    git("push", "-q", "-u", "origin", "pushed")
    if merge == "fast-forward":
        git("merge", "-q", "--ff-only", "pushed")
    elif merge == "rebase":
        git("commit", "-q", "--allow-empty", "-m", "upstream moved on")
        git("cherry-pick", "--allow-empty", work)
    if merge != "unmerged":
        git("push", "-q", "origin", "main")
    # Upstream deletes the branch; the prune removes the tracking ref but keeps the upstream config.
    git("push", "-q", "origin", "--delete", "pushed")
    git("fetch", "-q", "--prune", "origin")
    assert git("for-each-ref", "refs/remotes/origin/pushed") == ""
    branch_file = home / "desktop-update.json"
    branch_file.write_text(json.dumps({"branch": "pushed"}))
    status = check_for_updates(install_root=root, home=home, branch_config_path=branch_file)
    if merge == "unmerged":
        assert status["branch"] == "pushed", status
        assert "error" not in status, status
        assert status["localOnly"] is True
        assert "not in main" in status["message"]
        assert json.loads(branch_file.read_text())["branch"] == "pushed"
    else:
        assert "error" not in status, status
        assert status["branch"] == "main"
        assert status["targetSha"] == git("rev-parse", "origin/main")
        assert json.loads(branch_file.read_text())["branch"] == "main"


def test_fork_local_head_counts_locally_when_github_cannot_compare(installation):
    """A fork-local HEAD 404s the GitHub compare -- the base SHA only exists in the
    fork -- which used to leave ``behind`` at the unknown-count sentinel and the
    Desktop panel blind. The checkout already carries the upstream history its
    updater fetched, so ancestry answers locally without touching the network."""
    from hermes_cli.source_check_fork import check_for_updates

    root, _linked, home, _base, _head, responses, requests, git = installation
    git("checkout", "-q", "-b", "local-work")
    git("commit", "-q", "--allow-empty", "-m", "fork work")
    git("checkout", "-q", "main")
    git("commit", "-q", "--allow-empty", "-m", "upstream one")
    git("commit", "-q", "--allow-empty", "-m", "upstream two")
    tip = git("rev-parse", "main")
    git("checkout", "-q", "local-work")
    # Only the branch tip is served; the compare of a fork-local base is a 404
    # (every unserved path is), like api.github.com answering for the real fork.
    # The never-pushed branch answers 422 -- the only code the check trusts to
    # mean a ref is gone, since a 404 could also be a private repository.
    responses["/repos/fixture/fork/commits/local-work"] = (
        422, {"message": "No commit found for SHA: local-work"})
    responses["/repos/fixture/fork/commits/main"] = (200, tip)

    status = check_for_updates(install_root=root, home=home, branch_config_path=home / "desktop-update.json")

    assert "error" not in status, status
    assert status["localOnly"] is True
    assert status["behind"] == 2
    assert [c["summary"] for c in status["commits"]] == ["upstream one", "upstream two"]
    assert all(c["sha"] and c["author"] == "Fixture" and c["at"] > 0 for c in status["commits"])
    # The local count answered first: no compare call was spent.
    assert not any("/compare/" in request for request in requests)


def test_target_tip_missing_locally_is_fetched_once_with_a_deadline(installation, tmp_path):
    """Upstream can move past the checkout's last fetch while the fork-local HEAD
    keeps the compare API at 404. One bounded fetch of the target branch fills
    the gap, so the check stays answerable instead of reporting an unknown count."""
    from hermes_cli.source_check_fork import check_for_updates

    root, _linked, home, _base, _head, _responses, requests, git = installation
    _bare_origin(installation)
    bare = home / "remote.git"
    git("checkout", "-q", "-b", "local-work")
    git("commit", "-q", "--allow-empty", "-m", "fork work")
    # Upstream advances in a second clone and pushes; this checkout never sees it.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    str(bare), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-q", "--allow-empty", "-m", "upstream one"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-q", "--allow-empty", "-m", "upstream two"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True)

    status = check_for_updates(install_root=root, home=home, branch_config_path=home / "desktop-update.json")

    assert "error" not in status, status
    assert status["localOnly"] is True
    assert status["behind"] == 2
    assert [c["summary"] for c in status["commits"]] == ["upstream one", "upstream two"]
    assert requests == []
