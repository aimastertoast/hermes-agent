import subprocess

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