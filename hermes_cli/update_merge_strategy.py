"""Backup branch + git merge + restore. See spec Section 2.3."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class MergeResult:
    """Outcome of a merge attempt.

    MERGED / CONFLICT / FAILED are exposed as class constants so callers can
    compare `result.outcome == MergeResult.MERGED` without importing a
    separate enum.
    """

    MERGED = "merged"
    CONFLICT = "conflict"
    FAILED = "failed"

    def __init__(
        self,
        outcome: str,
        conflicted_files: Optional[list[str]] = None,
        error_detail: str = "",
    ):
        self.outcome = outcome
        self.conflicted_files = conflicted_files if conflicted_files is not None else []
        self.error_detail = error_detail


def backup_branch(repo: Path, ts: int, channel: str = "main") -> str:
    """Create backup-<ts> if local has commits ahead of origin/<channel>.

    The channel argument MUST match the channel the caller is about to merge
    (see :func:`attempt_merge`): on a non-``main`` channel the orchestrator
    pulls and merges ``origin/<channel>``, so the backup must count local
    commits ahead of the SAME ref. A hard-coded ``origin/main`` here meant a
    canary install whose local branch is at ``origin/main`` but ahead of
    ``origin/canary`` would create a needless backup branch — and worse, an
    install that IS ahead of ``origin/main`` but not ``origin/canary`` would
    silently skip the backup right when the local-ahead protection is most
    needed. Defaults to ``"main"`` so legacy callers (and tests that don't
    care about channel scoping) keep their behavior.

    Returns branch name, or empty string if no local commits to back up.
    """
    branch_name = f"backup-{ts}"
    # Check for local commits ahead of origin/<channel>
    ahead = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", f"origin/{channel}..HEAD"],
        capture_output=True, text=True,
    )
    if ahead.returncode != 0 or int(ahead.stdout.strip() or "0") == 0:
        return ""
    result = subprocess.run(
        ["git", "-C", str(repo), "branch", branch_name],
        capture_output=True, text=True,
    )
    return branch_name if result.returncode == 0 else ""


def attempt_merge(repo: Path, channel: str, backup_ref: str) -> MergeResult:
    """Fetch origin and merge origin/<channel> into HEAD."""
    fetch = subprocess.run(
        ["git", "-C", str(repo), "fetch", "origin", channel],
        capture_output=True, text=True,
    )
    if fetch.returncode != 0:
        return MergeResult(MergeResult.FAILED, error_detail=fetch.stderr.strip())

    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", f"origin/{channel}"],
        capture_output=True, text=True,
    )
    if merge.returncode == 0:
        return MergeResult(MergeResult.MERGED)

    # Detect conflict vs other failure
    if "CONFLICT" in merge.stdout or "conflict" in merge.stdout.lower():
        conflicted = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=U"],
            capture_output=True, text=True,
        ).stdout.strip().splitlines()
        return MergeResult(MergeResult.CONFLICT, conflicted_files=conflicted)

    return MergeResult(MergeResult.FAILED, error_detail=merge.stderr.strip())


def restore_backup_branch(repo: Path, backup_ref: str) -> bool:
    """Reset HEAD to backup_ref. Returns True iff successful."""
    if not backup_ref:
        return False
    result = subprocess.run(
        ["git", "-C", str(repo), "reset", "--hard", backup_ref],
        capture_output=True, text=True,
    )
    return result.returncode == 0