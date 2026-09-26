"""Image-managed refusal contract tests (#91277 Phase 3).

Marker semantics (image_provenance.py, salvaged from #92545 @andrexibiza):
absent → None; present-and-valid → provenance; present-but-broken →
fail-closed invalid. Admission gate (update_contract.py): marker first,
docker/nix/apt heuristics second; refusals record a `refused` receipt.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_cli.image_provenance import read_image_provenance
from hermes_cli.update_contract import (
    LOCAL_AHEAD_REFUSAL,
    FORCE_PUSHED_REFUSAL,
    LAST_ORIGIN_SHA_PATH,
    UpdateRefusal,
    evaluate_update_admission,
    evaluate_update_force_requirement,
    record_refusal_receipt,
)


# ---------------------------------------------------------------------------
# Hermetic git-repo helper (Task 8 force-requirement tests).
#
# Mirrors the `hermetic_git_repo` fixture in hermes_cli/tests/update/conftest.py
# but as a plain function: pytest's modern fixture-call-directly checks block
# importing fixtures across directories. The behavior is identical: bare
# remote at <tmp>/remote.git, clone at <tmp>/repo, three commits on `main`.
# ---------------------------------------------------------------------------


@dataclass
class _GitRepo:
    repo: Path
    remote: Path


def _make_hermetic_git_repo(tmp_path: Path) -> _GitRepo:
    repo_path = tmp_path / "repo"
    remote_path = tmp_path / "remote.git"
    subprocess.check_call(["git", "init", "--bare", str(remote_path)])
    subprocess.check_call(
        ["git", "-C", str(remote_path), "symbolic-ref", "HEAD", "refs/heads/main"]
    )
    subprocess.check_call(["git", "clone", str(remote_path), str(repo_path)])
    subprocess.check_call(
        ["git", "-C", str(repo_path), "config", "user.email", "test@fake.local"]
    )
    subprocess.check_call(
        ["git", "-C", str(repo_path), "config", "user.name", "Test User"]
    )
    for msg in ("initial", "second", "third"):
        (repo_path / f"file-{msg}.txt").write_text(msg)
        subprocess.check_call(["git", "-C", str(repo_path), "add", "."])
        subprocess.check_call(["git", "-C", str(repo_path), "commit", "-m", msg])
        subprocess.check_call(["git", "-C", str(repo_path), "push", "origin", "main"])
    return _GitRepo(repo=repo_path, remote=remote_path)


def _isolated_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HERMES_HOME at a tmp_path-anchored fake home so the SHA cache file
    never touches the user's live install."""
    home = tmp_path / "fake-home"
    (home / "update").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _valid_marker(tmp_path: Path) -> Path:
    marker = tmp_path / "image-provenance.json"
    marker.write_text(json.dumps({
        "schema": 1,
        "deployment_kind": "image",
        "manager": "docker",
        "image": "nousresearch/hermes-agent",
        "version": "1.0.0",
        "revision": "a" * 40,
    }))
    return marker


# ---------------------------------------------------------------------------
# Marker reader
# ---------------------------------------------------------------------------


def test_reader_absent_marker_means_none(tmp_path):
    assert read_image_provenance(tmp_path / "nope.json") is None


def test_reader_valid_marker(tmp_path):
    provenance = read_image_provenance(_valid_marker(tmp_path))
    assert provenance is not None and provenance.valid
    assert provenance.manager == "docker"
    assert provenance.version == "1.0.0"


@pytest.mark.parametrize(
    "payload,reason_prefix",
    [
        ("not json {", "marker_unreadable"),
        (json.dumps([1, 2]), "marker_not_object"),
        (json.dumps({"schema": True, "deployment_kind": "image", "manager": "docker"}), "unsupported_marker_schema"),
        (json.dumps({"schema": 2, "deployment_kind": "image", "manager": "docker"}), "unsupported_marker_schema"),
        (json.dumps({"schema": 1, "deployment_kind": "source", "manager": "docker"}), "invalid_deployment_kind"),
        (json.dumps({"schema": 1, "deployment_kind": "image", "manager": "  "}), "missing_manager"),
    ],
)
def test_reader_fails_closed_on_malformed(tmp_path, payload, reason_prefix):
    marker = tmp_path / "image-provenance.json"
    marker.write_text(payload)
    provenance = read_image_provenance(marker)
    assert provenance is not None and not provenance.valid
    assert provenance.error.startswith(reason_prefix)


def test_reader_rejects_symlink_marker(tmp_path):
    real = _valid_marker(tmp_path)
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    provenance = read_image_provenance(link)
    assert provenance is not None and not provenance.valid
    assert provenance.error == "marker_not_regular_file"


# ---------------------------------------------------------------------------
# Admission gate
# ---------------------------------------------------------------------------


def test_admission_marker_refuses_even_on_git_checkout(tmp_path, monkeypatch):
    """The bind-mounted-checkout case: heuristics say git, marker says image
    — the marker wins."""
    import hermes_cli.image_provenance as ip

    monkeypatch.setattr(ip, "IMAGE_PROVENANCE_PATH", _valid_marker(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda *a, **k: "git"
    )
    refusal = evaluate_update_admission(tmp_path)
    assert refusal is not None
    assert refusal.code == "image-marker"
    assert "docker pull" in refusal.update_command


def test_admission_invalid_marker_fails_closed(tmp_path, monkeypatch):
    import hermes_cli.image_provenance as ip

    bad = tmp_path / "image-provenance.json"
    bad.write_text("corrupted {{{")
    monkeypatch.setattr(ip, "IMAGE_PROVENANCE_PATH", bad)
    refusal = evaluate_update_admission(tmp_path)
    assert refusal is not None
    assert refusal.code == "image-marker-invalid"
    assert "docker pull" in refusal.update_command


def test_admission_no_marker_falls_back_to_heuristics(tmp_path, monkeypatch):
    import hermes_cli.image_provenance as ip

    monkeypatch.setattr(ip, "IMAGE_PROVENANCE_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda *a, **k: "docker"
    )
    refusal = evaluate_update_admission(tmp_path)
    assert refusal is not None and refusal.code == "docker"


def test_admission_git_checkout_no_marker_is_admitted(tmp_path, monkeypatch):
    import hermes_cli.image_provenance as ip

    monkeypatch.setattr(ip, "IMAGE_PROVENANCE_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda *a, **k: "git"
    )
    assert evaluate_update_admission(tmp_path) is None


def test_admission_nix_refuses(tmp_path, monkeypatch):
    import hermes_cli.image_provenance as ip

    monkeypatch.setattr(ip, "IMAGE_PROVENANCE_PATH", tmp_path / "absent.json")
    def _detect(*a, **k):
        return "nix"

    monkeypatch.setattr("hermes_cli.config.detect_install_method", _detect)
    refusal = evaluate_update_admission(tmp_path)
    assert refusal is not None and refusal.code == "nix"


# ---------------------------------------------------------------------------
# Refusal receipt
# ---------------------------------------------------------------------------


def test_refusal_receipt_written_as_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.update_receipt as ur

    monkeypatch.setattr(ur, "_receipt_dir", lambda: tmp_path / "receipts")

    record_refusal_receipt(
        UpdateRefusal(
            code="image-marker",
            message="msg",
            update_command="docker pull nousresearch/hermes-agent:latest",
        )
    )
    receipts = list((tmp_path / "receipts").glob("*.json"))
    receipts = [p for p in receipts if p.name != "latest.json"]
    assert receipts, "a refusal receipt must be written"
    data = json.loads(receipts[0].read_text())
    assert data["outcome"] == "refused"
    assert data["stop_reason"] == "image-marker"
    steps = {s["name"]: s for s in data["steps"]}
    assert "admission" in steps and steps["admission"]["ok"] is False
    assert "docker pull" in steps["admission"]["detail"]


# ---------------------------------------------------------------------------
# Sealed-steward admission gate: apt-termux
# ---------------------------------------------------------------------------


def _sealed_tree(tmp_path: Path, distribution: str) -> Path:
    """A sealed (no .git) tree stamped with ``distribution``."""
    root = tmp_path / "sealed"
    root.mkdir(parents=True, exist_ok=True)
    (root / "install-stamp.json").write_text(
        json.dumps({"distribution": distribution})
    )
    return root


def test_admission_source_checkout_on_termux_host_refuses_with_apt_hint(tmp_path, monkeypatch):
    """A git checkout is normally admitted, but not on a Termux host: the
    lock has no Android wheels, so a source sync would build sdists on the
    phone. The refusal must point at the APT package, never `hermes update`."""
    import hermes_cli.image_provenance as ip

    monkeypatch.setattr(ip, "IMAGE_PROVENANCE_PATH", tmp_path / "absent.json")
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    refusal = evaluate_update_admission(tmp_path)
    assert refusal is not None and refusal.code == "apt-termux"
    assert "pkg install hermes-agent" in refusal.message
    assert refusal.update_command == "pkg install hermes-agent"
    assert "hermes update" not in refusal.update_command
    monkeypatch.setenv("PREFIX", "/usr")
    assert evaluate_update_admission(tmp_path) is None, "the same checkout off Termux stays updatable"


def test_admission_apt_termux_refuses_with_pkg_upgrade(tmp_path, monkeypatch):
    """A sealed apt-termux tree (no .git) is refused by the steward gate:
    the package manager owns the code tree, so remediation is pkg upgrade
    — never `hermes update`."""
    import hermes_cli.image_provenance as ip

    monkeypatch.setattr(ip, "IMAGE_PROVENANCE_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda *a, **k: "git"
    )
    root = _sealed_tree(tmp_path, "apt-termux")
    refusal = evaluate_update_admission(root)
    assert refusal is not None
    assert refusal.code == "apt-termux"
    assert "pkg upgrade hermes-agent" in refusal.message
    assert "pkg upgrade hermes-agent" in refusal.update_command


def test_admission_apt_termux_command_comes_from_steward_table(tmp_path, monkeypatch):
    """The apt-termux remediation command is read from the config module's
    ``_UPDATE_COMMAND_BY_METHOD`` table (the same one every install method
    reads) — not hardcoded inline in the steward refusal — so there is ONE
    source of truth for the update command."""
    import hermes_cli.config as config_mod
    import hermes_cli.image_provenance as ip

    monkeypatch.setattr(ip, "IMAGE_PROVENANCE_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda *a, **k: "git"
    )
    # Prove the table is the source: a table edit flows into the refusal.
    monkeypatch.setitem(
        config_mod._UPDATE_COMMAND_BY_METHOD,
        "apt",
        "pkg upgrade hermes-agent --from-table",
    )
    root = _sealed_tree(tmp_path, "apt-termux")
    refusal = evaluate_update_admission(root)
    assert refusal is not None
    assert refusal.code == "apt-termux"
    assert refusal.update_command == "pkg upgrade hermes-agent --from-table"


# ---------------------------------------------------------------------------
# Force-requirement gate (Task 8 — local-ahead / force-pushed)
# ---------------------------------------------------------------------------
#
# These exercise ``evaluate_update_force_requirement``, which is intentionally
# separate from ``evaluate_update_admission`` (the install-method gate). The
# admission gate says "this install must not update in place" (Docker, Nix,
# apt, commit-build, sealed stewards); the force-requirement gate says "an
# update COULD proceed, but only with explicit user confirmation". Both
# refuse-shaped outcomes use the same ``UpdateRefusal`` dataclass so callers
# can branch on ``refusal.code``.


def test_force_requirement_local_ahead_triggers_refusal(tmp_path, monkeypatch):
    """A local commit on top of an in-sync origin triggers LOCAL_AHEAD_REFUSAL.

    The local commit has not been pushed, so ``rev-list origin/main..HEAD``
    counts >= 1 and the gate fires. The message must reference the Settings
    toggle (Task 12 wires the actual toggle; Task 8 only requires the message
    to mention it)."""
    _isolated_hermes_home(tmp_path, monkeypatch)
    git_repo = _make_hermetic_git_repo(tmp_path)
    repo = git_repo.repo

    # First call seeds the SHA cache and confirms the clean state.
    assert evaluate_update_force_requirement(repo) is None

    (repo / "local.txt").write_text("local-only")
    subprocess.check_call(["git", "-C", str(repo), "add", "."])
    subprocess.check_call(["git", "-C", str(repo), "commit", "-m", "local-only"])

    refusal = evaluate_update_force_requirement(repo)
    assert refusal is not None
    assert refusal.code == "local-ahead"
    assert refusal is LOCAL_AHEAD_REFUSAL
    assert "Settings" in refusal.message
    assert "Allow Update Now when local is ahead" in refusal.message


def test_force_requirement_no_local_ahead_returns_none(tmp_path, monkeypatch):
    """A clean repo (HEAD == origin/main, cache fresh) returns None."""
    _isolated_hermes_home(tmp_path, monkeypatch)
    git_repo = _make_hermetic_git_repo(tmp_path)
    refusal = evaluate_update_force_requirement(git_repo.repo)
    assert refusal is None

    # Second call also returns None (cache hit, no change).
    refusal = evaluate_update_force_requirement(git_repo.repo)
    assert refusal is None


def test_force_requirement_no_origin_returns_none(tmp_path, monkeypatch):
    """A repo with no ``origin`` remote cannot be compared against upstream
    and the gate must skip the check rather than refuse on infrastructure
    uncertainty."""
    _isolated_hermes_home(tmp_path, monkeypatch)
    git_repo = _make_hermetic_git_repo(tmp_path)
    repo = git_repo.repo
    subprocess.check_call(["git", "-C", str(repo), "remote", "remove", "origin"])

    # Even with a local-only commit, the missing origin means we skip the
    # check entirely rather than refuse.
    (repo / "local.txt").write_text("local-only")
    subprocess.check_call(["git", "-C", str(repo), "add", "."])
    subprocess.check_call(["git", "-C", str(repo), "commit", "-m", "local-only"])

    assert evaluate_update_force_requirement(repo) is None


def test_force_requirement_force_pushed_detection_via_stale_origin_sha(
    tmp_path, monkeypatch
):
    """A force-push that orphans local HEAD (the upstream history was rewritten
    so HEAD is no longer reachable from the new origin tip) triggers
    FORCE_PUSHED_REFUSAL even when ``rev-list origin/main..HEAD`` would also
    be non-zero. The force-pushed code takes precedence because a history
    rewrite is the more dangerous condition.

    Setup: rewrite origin's HEAD via ``commit --amend`` + ``push --force``
    from a sibling clone, then ``fetch`` in the original repo so its
    ``origin/main`` ref reflects the rewrite. The cache is pre-populated with
    the SHA we recorded BEFORE the rewrite so the SHA-comparison arm fires.
    """
    home = _isolated_hermes_home(tmp_path, monkeypatch)
    git_repo = _make_hermetic_git_repo(tmp_path)
    repo = git_repo.repo

    # First seed the cache with the *current* SHA so the next rewrite is
    # detected as a change rather than a first-run no-op.
    seeded = evaluate_update_force_requirement(repo)
    assert seeded is None
    cache_file = home / "update" / "last_origin_sha"
    assert cache_file.exists()
    pre_rewrite_sha = cache_file.read_text().strip()

    # Force-push from a sibling clone: amend the latest commit then push --force.
    rewriter = tmp_path / "rewriter"
    subprocess.check_call(["git", "clone", str(git_repo.remote), str(rewriter)])
    subprocess.check_call(
        ["git", "-C", str(rewriter), "config", "user.email", "test@fake.local"]
    )
    subprocess.check_call(
        ["git", "-C", str(rewriter), "config", "user.name", "Test User"]
    )
    (rewriter / "file-third.txt").write_text("amended-content")
    subprocess.check_call(["git", "-C", str(rewriter), "add", "."])
    subprocess.check_call(
        ["git", "-C", str(rewriter), "commit", "--amend", "--no-edit"]
    )
    subprocess.check_call(
        ["git", "-C", str(rewriter), "push", "--force", "origin", "main"]
    )
    subprocess.check_call(["git", "-C", str(repo), "fetch", "origin"])

    # Sanity: confirm we actually rewrote history (origin SHA now differs
    # from what we recorded). This guards against the test silently passing
    # because the amend/fetch sequence didn't actually change anything.
    new_origin_sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "origin/main"], text=True
    ).strip()
    assert new_origin_sha != pre_rewrite_sha, "force-push setup did not rewrite origin"

    refusal = evaluate_update_force_requirement(repo)
    assert refusal is not None
    assert refusal.code == "force-pushed"
    assert refusal is FORCE_PUSHED_REFUSAL
    assert "force-push" in refusal.message.lower()


def test_force_requirement_last_origin_sha_path_default():
    """Sanity: the module-level default points under ``~/.hermes/update``."""
    assert str(LAST_ORIGIN_SHA_PATH).endswith(
        os.path.join(".hermes", "update", "last_origin_sha")
    )
