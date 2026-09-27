"""Image-managed install refusal contract.

A refusal prints the real update command for the deployment kind, records a
``refused`` receipt (so fleet tooling sees "this install cannot self-update,
use <command>" instead of a silent non-update), and exits 2 on CLI surfaces.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from hermes_cli.steward import STEWARD_APT_TERMUX, STEWARD_DESKTOP, STEWARD_DOCKER, STEWARD_NIX

logger = logging.getLogger(__name__)

COMMIT_BUILD_UPDATE_MESSAGE = (
    "This build doesn't get updates. Ask the developer who gave it to you for a new build."
)

# Default path for the "last-known origin SHA" cache used by
# ``evaluate_update_force_requirement`` to detect a force-pushed upstream.
# Tests override this by setting ``HERMES_HOME``; production reads from
# ``~/.hermes/update/last_origin_sha``.
LAST_ORIGIN_SHA_PATH = Path("~/.hermes/update/last_origin_sha").expanduser()


def is_commit_build(project_root: Path) -> bool:
    from hermes_cli.steward import read_install_stamp

    return read_install_stamp(project_root).get("source") == "commit-build"


@dataclass(frozen=True)
class UpdateRefusal:
    """Why an in-place update is refused, and what to run instead."""

    code: str              # image-marker | image-marker-invalid | docker | nix | apt | desktop-app | <steward>
    message: str           # full user-facing text (multi-line ok)
    update_command: str    # the one-line remediation command


def _refusal(code: str, method: str, message: Optional[Callable[[str], str]] = None) -> UpdateRefusal:
    """Refusal for ``method``: ``message(command)`` if given, else docker's full message / the bare command."""
    from hermes_cli.config import format_docker_update_message, recommended_update_command_for_method

    command = recommended_update_command_for_method(method)
    if message is not None:
        text = message(command)
    else:
        text = format_docker_update_message() if method == "docker" else command
    return UpdateRefusal(code=code, message=text, update_command=command)


# Sealed-tree steward -> install method whose CLI command remediates it. The
# refusal code is the steward name itself. APT owns the Termux code tree:
# ``pkg upgrade`` replaces it wholesale, so it must never update in place.
_STEWARD_UPDATE_METHODS: dict[str, str] = {
    STEWARD_DOCKER: "docker",
    STEWARD_NIX: "nix",
    STEWARD_APT_TERMUX: "apt",
}


def _steward_refusal(steward: str) -> UpdateRefusal:
    """Refusal for a tree sealed by ``steward``."""
    from hermes_cli.steward import steward_update_message

    method = _STEWARD_UPDATE_METHODS.get(steward)
    if method == "docker":
        return _refusal(steward, method)
    if method is not None:
        return _refusal(steward, method, lambda _command: steward_update_message(steward))
    # desktop-app and future package managers have no CLI remediation: the
    # steward's own instructions are the remediation, and
    # recommended_update_command_for_method would falsely answer "hermes
    # update" for methods it doesn't know.
    command = (
        "Manage updates from within the desktop app"
        if steward == STEWARD_DESKTOP
        else f"update via {steward}"
    )
    return UpdateRefusal(code=steward, message=steward_update_message(steward), update_command=command)


def evaluate_update_admission(project_root: Path) -> Optional[UpdateRefusal]:
    """Return an :class:`UpdateRefusal` when in-place update must not run.

    ``None`` means the install is eligible for in-place update (git checkout or unknown-but-
    mutable). Never raises; on any internal error it falls back to the heuristic layer only.
    """
    if is_commit_build(project_root):
        return UpdateRefusal("commit-build", COMMIT_BUILD_UPDATE_MESSAGE, "")

    # Layer 1: baked provenance marker — authoritative when present.
    try:
        from hermes_cli.image_provenance import read_image_provenance

        provenance = read_image_provenance()
        if provenance is not None:
            if not provenance.valid:
                # Present but malformed: still image-managed — an integrity defect is never
                # permission to mutate the image in place.
                return _refusal("image-marker-invalid", "docker", lambda command: (
                    "✗ This install is image-managed, but its provenance "
                    f"marker is invalid ({provenance.error}).\n"
                    "  In-place update is disabled. Update by pulling a "
                    f"new image:\n    {command}"
                ))
            return _refusal("image-marker", provenance.manager)
    except Exception as exc:
        logger.debug("Image provenance check failed (using heuristics): %s", exc)

    # Layer 2: install stamp / steward classification. A sealed tree (no
    # ``.git``) belongs to a steward — the desktop app bundle, a Docker
    # image, the Nix store — and only the steward updates it. This is the
    # rung that covers ``desktop-app``, which the heuristics below never
    # detect (the payload has no .install_method stamp and no .git).
    try:
        from hermes_cli.steward import sealed_steward

        steward = sealed_steward(project_root)
        if steward is None:
            from hermes_constants import is_termux

            if is_termux():
                from hermes_cli.steward import SOURCE_ON_TERMUX_UPDATE_COMMAND, SOURCE_ON_TERMUX_UPDATE_MESSAGE

                return UpdateRefusal(
                    code=STEWARD_APT_TERMUX,
                    message=SOURCE_ON_TERMUX_UPDATE_MESSAGE,
                    update_command=SOURCE_ON_TERMUX_UPDATE_COMMAND,
                )
        elif steward != "unknown":
            return _steward_refusal(steward)
    except Exception as exc:
        logger.debug("Steward admission check failed: %s", exc)

    # Layer 3: pre-existing filesystem heuristics, verbatim semantics.
    try:
        from hermes_cli.config import detect_install_method, is_nix_install_method

        method = detect_install_method(project_root)
        if method == "docker":
            return _refusal("docker", method)
        if is_nix_install_method(method) or method == "apt":
            return _refusal(method if method == "apt" else "nix", method)
    except Exception as exc:
        logger.debug("Install-method admission check failed: %s", exc)
    return None


def record_refusal_receipt(refusal: UpdateRefusal) -> None:
    """Write a minimal ``refused`` receipt for a blocked update attempt.

    Gives fleet tooling a durable record that an update was ATTEMPTED and refused ("not updatable in
    place, use <command>") instead of a silent nothing. Best-effort; never raises.
    """
    try:
        from hermes_cli.update_receipt import begin_update_receipt, finalize_update_receipt, record_step

        begin_update_receipt()
        detail = f"not updatable in place ({refusal.code})"
        detail += f"; use: {refusal.update_command}" if refusal.update_command else f"; {refusal.message}"
        record_step("admission", False, detail)
        finalize_update_receipt("refused", stop_reason=refusal.code)
    except Exception as exc:
        logger.debug("Could not record refusal receipt: %s", exc)


# ---------------------------------------------------------------------------
# Force-mode refusals (local-ahead / force-pushed)
# ---------------------------------------------------------------------------
#
# These are NOT install-method refusals (Docker / Nix / apt / commit-build) —
# those live in ``evaluate_update_admission`` and mean "do not update in place".
# Force-mode refusals mean "an update COULD proceed, but only with explicit
# force". A separate gate (``Settings → Updates → Allow Update Now when local
# is ahead`` — wired in Task 12) decides whether the user has armed force mode.
# Until that gate exists, ``evaluate_update_force_requirement`` is detection-
# only: callers decide what to do with the refusal.

LOCAL_AHEAD_REFUSAL = UpdateRefusal(
    code="local-ahead",
    message=(
        "You have commits ahead of upstream. By default, Update Now is disabled "
        "to protect your local changes. Enable 'Allow Update Now when local is ahead' "
        "in Settings → Updates to proceed (a backup branch will be created first)."
    ),
    update_command="",
)

FORCE_PUSHED_REFUSAL = UpdateRefusal(
    code="force-pushed",
    message=(
        "Origin was force-pushed. Update is paused. Run `git fetch origin` to see "
        "the new history, then retry."
    ),
    update_command="",
)


def _resolve_last_origin_sha_path() -> Path:
    """Resolve the cache file path. Honors ``HERMES_HOME`` for test isolation.

    Falls back to :data:`LAST_ORIGIN_SHA_PATH` (``~/.hermes/update/last_origin_sha``)
    when ``HERMES_HOME`` is unset.
    """
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home) / "update" / "last_origin_sha"
    return LAST_ORIGIN_SHA_PATH


def _detect_current_branch(repo: Path) -> Optional[str]:
    """Return the current branch name, or ``None`` if detached / unknown."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    if not name or name == "HEAD":
        return None
    return name


def _has_origin_remote(repo: Path) -> bool:
    """True iff ``origin`` is configured for this repo."""
    result = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def _count_local_commits_ahead(repo: Path) -> Optional[int]:
    """Count commits on local HEAD not in ``origin/<branch>``.

    Returns ``None`` if there is no origin or the branch can't be resolved
    (skip the check rather than refuse on infrastructure uncertainty).
    """
    if not _has_origin_remote(repo):
        return None
    branch = _detect_current_branch(repo)
    if branch is None:
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", f"origin/{branch}..HEAD"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _read_origin_sha(repo: Path, branch: str) -> Optional[str]:
    """Read the current ``origin/<branch>`` SHA. ``None`` if unknown."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"origin/{branch}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _read_last_known_origin_sha() -> Optional[str]:
    """Read the cached last-known origin SHA. ``None`` if the cache is absent / unreadable."""
    path = _resolve_last_origin_sha_path()
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _write_last_known_origin_sha(sha: str) -> None:
    """Persist the current origin SHA to the cache. Best-effort, never raises."""
    path = _resolve_last_origin_sha_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sha)
    except OSError as exc:
        logger.debug("Could not write last-origin-sha cache: %s", exc)


def _is_origin_force_pushed(repo: Path) -> bool:
    """Detect a force-pushed upstream.

    Compares the cached last-known ``origin/<branch>`` SHA to the current one.
    A real force-push also means local HEAD is NOT in the new origin's history
    (``merge-base --is-ancestor HEAD origin/<branch>`` returns non-zero) — a
    linear fast-forward from the user's perspective would still leave HEAD as
    an ancestor of the new tip, so we don't flag that. Updates the cache to
    the current SHA on every call so subsequent runs compare against the
    latest known state.
    """
    branch = _detect_current_branch(repo)
    if branch is None:
        return False
    if not _has_origin_remote(repo):
        return False
    current_sha = _read_origin_sha(repo, branch)
    if current_sha is None:
        return False
    last_sha = _read_last_known_origin_sha()

    if last_sha is None or last_sha == current_sha:
        # First run, or nothing changed: refresh the cache and move on.
        _write_last_known_origin_sha(current_sha)
        return False

    # SHA differs. Distinguish a real rewrite from a linear fast-forward: in a
    # rewrite, local HEAD is no longer reachable from the new origin tip.
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}"],
        capture_output=True, text=True, check=False,
    )
    is_local_orphaned = ancestor.returncode != 0
    # Update the cache either way — the SHA we just observed is the new baseline.
    _write_last_known_origin_sha(current_sha)
    return is_local_orphaned


def evaluate_update_force_requirement(
    project_root: Path,
    *,
    home: Optional[Path] = None,
) -> Optional[UpdateRefusal]:
    """Return an :class:`UpdateRefusal` when the user should be required to force.

    Distinct from :func:`evaluate_update_admission`, which refuses install
    methods that must not update in place (Docker, Nix, apt, commit-build,
    sealed stewards). This function detects *user-side* states where an
    update could technically proceed but should require an explicit force
    confirmation:

    - ``force-pushed`` — upstream history was rewritten; checked first because
      it is the more dangerous of the two. Never bypassed by user toggles
      (force-pushed is a server-side event the user cannot remediate from
      the settings panel).
    - ``local-ahead`` — local has commits not on upstream. Bypassed when the
      desktop's ``$updateAllowLocalAhead`` toggle is armed (the orchestrator
      creates a backup branch first so the user can recover).

    Reads the toggle from ``HERMES_HOME/update_settings.json`` via
    :func:`hermes_cli.update_settings.load_update_settings`. When ``home`` is
    not provided, falls back to ``project_root.parent`` — the closest
    reasonable default for a git checkout whose parent directory happens to
    be ``HERMES_HOME``. Callers should pass an explicit ``home`` when they
    have one (the orchestrator always does).

    Returns ``None`` if a normal (non-forced) update is fine. Never raises;
    on any internal error it fails OPEN (returns ``None``) so we don't block
    legitimate updates on transient infrastructure problems.
    """
    try:
        if _is_origin_force_pushed(project_root):
            return FORCE_PUSHED_REFUSAL
    except Exception as exc:
        logger.debug("Force-pushed check failed (failing open): %s", exc)

    try:
        ahead = _count_local_commits_ahead(project_root)
        if ahead is not None and ahead > 0:
            # The local-ahead gate respects the desktop's toggle. A force-pushed
            # upstream is NEVER bypassed — that's a server-side event whose
            # only correct response is for the user to re-fetch and reconcile,
            # not for a "force through" toggle.
            if _is_local_ahead_bypass_armed(home, project_root):
                logger.debug(
                    "Local-ahead refusal suppressed by $updateAllowLocalAhead toggle "
                    "(%d unpushed commit(s)); backup branch will be created.",
                    ahead,
                )
                return None
            return LOCAL_AHEAD_REFUSAL
    except Exception as exc:
        logger.debug("Local-ahead check failed (failing open): %s", exc)

    return None


def _is_local_ahead_bypass_armed(
    home: Optional[Path], project_root: Path
) -> bool:
    """Return True iff the desktop's local-ahead bypass toggle is armed.

    Reads ``HERMES_HOME/update_settings.json`` when ``home`` is given, else
    falls back to ``project_root.parent`` (best-effort: a git checkout whose
    parent directory is the active ``HERMES_HOME``). Never raises — a
    missing/corrupt/unreadable file counts as "not armed".
    """
    if home is None:
        # Best-effort default; callers should pass home explicitly. We pick
        # project_root.parent because the orchestrator's repo path is usually
        # HERMES_HOME/repo in a git install, so HERMES_HOME is the parent.
        home = project_root.parent

    try:
        from hermes_cli.update_settings import load_update_settings
        settings = load_update_settings(home)
    except Exception as exc:
        logger.debug("update_settings load failed (failing closed): %s", exc)
        return False

    if settings is None:
        return False
    return settings.allow_local_ahead
