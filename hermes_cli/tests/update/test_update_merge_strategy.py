import subprocess
from pathlib import Path

from hermes_cli.update_merge_strategy import (
    backup_branch, attempt_merge, restore_backup_branch, MergeResult,
)


def test_backup_branch_skipped_when_no_local_commits(hermetic_git_repo):
    name = backup_branch(hermetic_git_repo.repo, ts=1700000000)
    assert name == ""


def test_backup_branch_created_when_local_ahead(hermetic_git_repo):
    repo = hermetic_git_repo.repo
    (repo / "local.txt").write_text("local")
    subprocess.check_call(["git", "-C", str(repo), "add", "."])
    subprocess.check_call(["git", "-C", str(repo), "commit", "-m", "local commit"])
    name = backup_branch(repo, ts=1700000000)
    assert name.startswith("backup-")
    branches = subprocess.check_output(
        ["git", "-C", str(repo), "branch"]
    ).decode()
    assert name in branches


class TestBackupBranchChannelAware:
    """M4: ``backup_branch`` must count local commits ahead of the SAME ref the
    orchestrator is about to merge (``origin/<channel>``), not a hardcoded
    ``origin/main``.

    Three scenarios pin the contract:

    1. **Local at canary, canary ahead of main** — local has 0 commits ahead
       of ``origin/canary``, but the OLD hardcoded ``origin/main..HEAD`` would
       count canary-only commits as "ahead" and create a wasted backup. The
       new code returns ``""``.
    2. **Local ahead of canary only** — local has commits not on ``origin/canary``,
       and those commits ARE the ones a canary merge would discard. The new
       code creates a backup branch.
    3. **Default channel is still ``"main"``** — legacy callers and tests that
       don't pass a channel keep their behavior.
    """

    @staticmethod
    def _seed_remote_branch(remote: Path, channel: str, file_name: str) -> None:
        """Materialize ``channel`` on the bare remote with one new commit."""
        subprocess.check_call(
            ["git", "-C", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{channel}"]
        )
        work = remote.parent / f"{channel}-work"
        if work.exists():
            subprocess.check_call(["rm", "-rf", str(work)])
        subprocess.check_call(["git", "clone", str(remote), str(work)])
        subprocess.check_call(
            ["git", "-C", str(work), "config", "user.email", "test@fake.local"]
        )
        subprocess.check_call(
            ["git", "-C", str(work), "config", "user.name", "Test User"]
        )
        (work / file_name).write_text(file_name)
        subprocess.check_call(["git", "-C", str(work), "add", "."])
        subprocess.check_call(["git", "-C", str(work), "commit", "-m", file_name])
        # Force-push so origin/<channel> diverges from origin/main with a
        # real commit that exists on the channel only.
        subprocess.check_call(
            ["git", "-C", str(work), "push", "--force", "origin", channel]
        )

    @staticmethod
    def _local_matches_channel(remote: Path, channel: str) -> None:
        """Move the local clone's HEAD to ``origin/<channel>`` so HEAD == channel."""
        repo = remote.parent / "repo"
        subprocess.check_call(["git", "-C", str(repo), "fetch", "origin", channel])
        # ``reset --hard origin/<channel>`` requires the channel ref locally.
        subprocess.check_call(
            ["git", "-C", str(repo), "reset", "--hard", f"origin/{channel}"]
        )

    def test_local_at_canary_with_canary_ahead_of_main_skips_backup(
        self, hermetic_git_repo
    ):
        """The bug scenario: canary has commits not in main; local matches
        canary exactly. A canary merge has nothing to back up — the OLD code
        would create a wasted backup branch because ``origin/main..HEAD``
        counted the canary-only commits as "ahead". The new code correctly
        scopes to ``origin/canary`` and returns ``""``."""
        repo = hermetic_git_repo.repo
        self._seed_remote_branch(hermetic_git_repo.remote, "canary", "canary-only.txt")
        self._local_matches_channel(hermetic_git_repo.remote, "canary")

        # Sanity: local really is at canary and canary really is ahead of main.
        ahead_of_canary = subprocess.check_output(
            ["git", "-C", str(repo), "rev-list", "--count", "origin/canary..HEAD"],
            text=True,
        ).strip()
        ahead_of_main = subprocess.check_output(
            ["git", "-C", str(repo), "rev-list", "--count", "origin/main..HEAD"],
            text=True,
        ).strip()
        assert int(ahead_of_canary) == 0, "local should be at origin/canary"
        assert int(ahead_of_main) > 0, "canary should be ahead of main"

        # New code: no backup needed.
        assert backup_branch(repo, ts=1700000010, channel="canary") == ""

    def test_local_ahead_of_canary_creates_backup(self, hermetic_git_repo):
        """The protection scenario: local has a commit not in ``origin/canary``.
        The new code counts it and creates a backup branch — this is the case
        the local-ahead protection exists to handle."""
        repo = hermetic_git_repo.repo
        self._seed_remote_branch(hermetic_git_repo.remote, "canary", "canary-only.txt")
        self._local_matches_channel(hermetic_git_repo.remote, "canary")

        # Local commit: ahead of canary (and also ahead of main, but that's
        # incidental — what matters is the canary-ahead check).
        (repo / "local.txt").write_text("local")
        subprocess.check_call(["git", "-C", str(repo), "add", "."])
        subprocess.check_call(["git", "-C", str(repo), "commit", "-m", "local ahead"])

        name = backup_branch(repo, ts=1700000011, channel="canary")
        assert name.startswith("backup-")
        branches = subprocess.check_output(
            ["git", "-C", str(repo), "branch"]
        ).decode()
        assert name in branches

    def test_default_channel_is_main(self, hermetic_git_repo):
        """Backwards-compat: a caller that omits ``channel`` still scopes against
        ``origin/main`` (legacy behavior preserved)."""
        repo = hermetic_git_repo.repo
        (repo / "local-default.txt").write_text("default")
        subprocess.check_call(["git", "-C", str(repo), "add", "."])
        subprocess.check_call(["git", "-C", str(repo), "commit", "-m", "ahead of main"])
        name = backup_branch(repo, ts=1700000012)
        assert name.startswith("backup-")


def test_attempt_merge_fast_forward(hermetic_git_repo):
    # Add a commit to origin and fetch
    subprocess.check_call(["git", "-C", str(hermetic_git_repo.remote), "symbolic-ref", "HEAD", "refs/heads/main"])
    other = hermetic_git_repo.remote.parent / "other"
    subprocess.check_call(["git", "clone", str(hermetic_git_repo.remote), str(other)])
    # Host adaptation: this machine has no global git user identity, and a
    # fresh clone inherits neither local nor remote identity. Configure the
    # cloned repo before committing (same kind of fix as the symbolic-ref).
    subprocess.check_call(["git", "-C", str(other), "config", "user.email", "test@fake.local"])
    subprocess.check_call(["git", "-C", str(other), "config", "user.name", "Test User"])
    (other / "remote-change.txt").write_text("remote")
    subprocess.check_call(["git", "-C", str(other), "add", "."])
    subprocess.check_call(["git", "-C", str(other), "commit", "-m", "remote"])
    subprocess.check_call(["git", "-C", str(other), "push", "origin", "main"])

    result = attempt_merge(hermetic_git_repo.repo, channel="main", backup_ref="")
    assert result.outcome == MergeResult.MERGED


def test_attempt_merge_conflict_preserves_repo(hermetic_git_repo):
    repo = hermetic_git_repo.repo
    # Local change
    (repo / "conflict.txt").write_text("local\n")
    subprocess.check_call(["git", "-C", str(repo), "add", "."])
    subprocess.check_call(["git", "-C", str(repo), "commit", "-m", "local edit"])

    # Remote change
    subprocess.check_call(["git", "-C", str(hermetic_git_repo.remote), "symbolic-ref", "HEAD", "refs/heads/main"])
    other = repo.parent / "other"
    subprocess.check_call(["git", "clone", str(hermetic_git_repo.remote), str(other)])
    # Host adaptation: see note in test_attempt_merge_fast_forward.
    subprocess.check_call(["git", "-C", str(other), "config", "user.email", "test@fake.local"])
    subprocess.check_call(["git", "-C", str(other), "config", "user.name", "Test User"])
    (other / "conflict.txt").write_text("remote\n")
    subprocess.check_call(["git", "-C", str(other), "add", "."])
    subprocess.check_call(["git", "-C", str(other), "commit", "-m", "remote edit"])
    subprocess.check_call(["git", "-C", str(other), "push", "origin", "main"])

    result = attempt_merge(repo, channel="main", backup_ref="backup-test")
    assert result.outcome == MergeResult.CONFLICT
    assert "conflict.txt" in result.conflicted_files
    # Repo is in MERGE_HEAD state (deliberate)
    assert (repo / ".git" / "MERGE_HEAD").exists()