"""Tests for ``update_settings`` (M2b) and the ``$updateAllowLocalAhead`` toggle
wiring into ``evaluate_update_force_requirement``.

Coverage:

1. ``load_update_settings`` correctly reads the desktop's mirrored settings.
2. Strict boolean parsing rejects ``"true"`` strings, ``1`` ints, etc. — only
   JSON true/false count, so a typo'd value can't accidentally arm the bypass.
3. Schema-version guard refuses unknown future schemas.
4. ``evaluate_update_force_requirement`` returns ``LOCAL_AHEAD_REFUSAL`` when
   local is ahead AND the toggle is OFF.
5. ``evaluate_update_force_requirement`` returns ``None`` when local is ahead
   AND the toggle is armed (``$updateAllowLocalAhead == true``).
6. ``evaluate_update_force_requirement`` returns ``FORCE_PUSHED_REFUSAL`` even
   when the toggle is armed — force-pushed is never bypassed.
7. ``write_update_settings`` + ``load_update_settings`` round-trips.

The toggle is wired through ``HERMES_HOME/update_settings.json`` (the
desktop's electron-main IPC mirrors atom changes here). Tests use isolated
tmp_path homes so they never touch the user's real ``~/.hermes``.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli.update_contract import (
    FORCE_PUSHED_REFUSAL,
    LOCAL_AHEAD_REFUSAL,
    evaluate_update_force_requirement,
)
from hermes_cli.update_settings import (
    SCHEMA_VERSION,
    UpdateSettings,
    load_update_settings,
    settings_path,
    write_update_settings,
)


# ─────────────────────────────────────────────────────────────────────────────
# Settings reader
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadUpdateSettings:
    """``load_update_settings`` reads the desktop-mirrored toggle file."""

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        """No file → None (caller uses defaults). Never raises."""
        assert load_update_settings(tmp_path) is None

    def test_returns_none_when_corrupt_json(self, tmp_path: Path) -> None:
        """Garbage in the file → None. Critical: a corrupt file must NOT
        silently flip the toggle to a default that arms the bypass."""
        settings_path(tmp_path).write_text("{not valid json")
        assert load_update_settings(tmp_path) is None

    def test_returns_none_when_root_not_an_object(self, tmp_path: Path) -> None:
        """JSON list at root is not a settings file."""
        settings_path(tmp_path).write_text("[1, 2, 3]")
        assert load_update_settings(tmp_path) is None

    def test_returns_none_when_toggles_missing(self, tmp_path: Path) -> None:
        settings_path(tmp_path).write_text(json.dumps({"schema_version": 1}))
        assert load_update_settings(tmp_path) is None

    def test_returns_none_when_newer_schema_version(self, tmp_path: Path) -> None:
        """Future files written by a newer desktop must not be misread."""
        payload = {
            "schema_version": SCHEMA_VERSION + 1,
            "toggles": {"allow_local_ahead": True, "safe_mode_auto": False},
        }
        settings_path(tmp_path).write_text(json.dumps(payload))
        assert load_update_settings(tmp_path) is None

    def test_returns_defaults_when_both_toggles_false(self, tmp_path: Path) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "toggles": {"allow_local_ahead": False, "safe_mode_auto": False},
        }
        settings_path(tmp_path).write_text(json.dumps(payload))
        settings = load_update_settings(tmp_path)
        assert settings == UpdateSettings(allow_local_ahead=False, safe_mode_auto=False)

    def test_returns_armed_when_toggle_true(self, tmp_path: Path) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "toggles": {"allow_local_ahead": True, "safe_mode_auto": False},
        }
        settings_path(tmp_path).write_text(json.dumps(payload))
        settings = load_update_settings(tmp_path)
        assert settings is not None
        assert settings.allow_local_ahead is True
        assert settings.safe_mode_auto is False

    @pytest.mark.parametrize(
        "value",
        [
            "true",         # JSON string, not bool
            "false",
            1,              # int, not bool
            0,
            "1",
            "yes",
            None,
            [],             # empty list
            {},
        ],
    )
    def test_strict_boolean_rejects_non_bool(self, tmp_path: Path, value) -> None:
        """A loose coercion (e.g. ``bool(value)``) would treat ``"false "``
        as truthy and silently arm the local-ahead bypass. The reader must
        refuse non-bool values."""
        payload = {
            "schema_version": SCHEMA_VERSION,
            "toggles": {"allow_local_ahead": value, "safe_mode_auto": False},
        }
        settings_path(tmp_path).write_text(json.dumps(payload))
        assert load_update_settings(tmp_path) is None


class TestWriteUpdateSettings:
    """Atomic write helper used by the desktop's electron-main IPC and tests."""

    def test_round_trip_preserves_values(self, tmp_path: Path) -> None:
        write_update_settings(
            tmp_path,
            UpdateSettings(allow_local_ahead=True, safe_mode_auto=True),
        )
        loaded = load_update_settings(tmp_path)
        assert loaded == UpdateSettings(allow_local_ahead=True, safe_mode_auto=True)

    def test_creates_home_directory_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "home"
        write_update_settings(nested, UpdateSettings(allow_local_ahead=True))
        assert (nested / "update_settings.json").exists()

    def test_atomic_rename_leaves_no_tmp(self, tmp_path: Path) -> None:
        """A failed write must not leave a half-written ``.tmp`` file that a
        concurrent orchestrator invoke could read."""
        write_update_settings(tmp_path, UpdateSettings(allow_local_ahead=True))
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name == "update_settings.json"


# ─────────────────────────────────────────────────────────────────────────────
# evaluate_update_force_requirement: toggle integration
# ─────────────────────────────────────────────────────────────────────────────

class TestForceRequirementToggle:
    """``$updateAllowLocalAhead`` toggle wires into the force-requirement gate.

    The toggle is mirrored to ``HERMES_HOME/update_settings.json`` by the
    desktop's electron-main IPC. The Python side reads it via
    ``load_update_settings(home)``.

    What MUST happen:

    - local-ahead, toggle OFF → ``LOCAL_AHEAD_REFUSAL``
    - local-ahead, toggle ON  → ``None`` (proceed; orchestrator creates backup)
    - force-pushed, toggle ON → ``FORCE_PUSHED_REFUSAL`` (NEVER bypassed)
    - clean state             → ``None``
    """

    def _make_local_ahead(self, repo: Path) -> None:
        """Create one local commit that's not pushed to origin/main."""
        (repo / "unpushed.txt").write_text("local")
        subprocess.check_call(["git", "-C", str(repo), "add", "."], cwd=str(repo))
        subprocess.check_call(
            ["git", "-C", str(repo), "commit", "-m", "unpushed"], cwd=str(repo)
        )
        # Intentionally do NOT push — local is ahead of origin/main.

    def _make_force_pushed(self, repo: Path, remote: Path) -> None:
        """Rewrite the remote's main to a brand-new commit (not reachable
        from the local clone's origin/main ref). The cache file also needs
        a prior SHA so the cache miss → new SHA → force-push detection."""
        # First, cache the current origin SHA so the detector sees a delta.
        cache_path = Path(os.environ["HERMES_HOME"]) / "update" / "last_origin_sha"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("0000000000000000000000000000000000000000\n")

        # Now rewrite the remote's main with a totally unrelated commit.
        orphan = repo.parent / "orphan"
        subprocess.check_call(["git", "clone", str(remote), str(orphan)])
        subprocess.check_call(
            ["git", "-C", str(orphan), "config", "user.email", "orphan@fake.local"],
            cwd=str(orphan),
        )
        subprocess.check_call(
            ["git", "-C", str(orphan), "config", "user.name", "Orphan"], cwd=str(orphan)
        )
        subprocess.check_call(
            ["git", "-C", str(orphan), "checkout", "--orphan", "orphan-branch"],
            cwd=str(orphan),
        )
        (orphan / "rewritten.txt").write_text("rewritten")
        subprocess.check_call(["git", "-C", str(orphan), "add", "."], cwd=str(orphan))
        subprocess.check_call(
            ["git", "-C", str(orphan), "commit", "-m", "force-pushed"],
            cwd=str(orphan),
        )
        subprocess.check_call(
            ["git", "-C", str(orphan), "push", "--force", "origin", "orphan-branch:main"],
            cwd=str(orphan),
        )
        # Local clone fetches the rewritten history.
        subprocess.check_call(["git", "-C", str(repo), "fetch", "origin"], cwd=str(repo))

    def test_local_ahead_returns_refusal_when_toggle_off(
        self, isolated_hermes_home: Path, hermetic_git_repo
    ) -> None:
        """Default install: toggle is OFF, so local-ahead blocks the update."""
        repo = hermetic_git_repo.repo
        self._make_local_ahead(repo)

        # Default: no update_settings.json on disk, so toggle is OFF.
        refusal = evaluate_update_force_requirement(repo, home=isolated_hermes_home)
        assert refusal is LOCAL_AHEAD_REFUSAL, (
            f"Expected LOCAL_AHEAD_REFUSAL with toggle off, got {refusal}"
        )

    def test_local_ahead_proceeds_when_toggle_on(
        self, isolated_hermes_home: Path, hermetic_git_repo
    ) -> None:
        """User armed 'Allow Update Now when local is ahead' → orchestrator
        proceeds past the gate. The orchestrator creates a backup branch
        first; the toggle only arms the bypass, not the safety net."""
        repo = hermetic_git_repo.repo
        self._make_local_ahead(repo)

        # User arms the toggle.
        write_update_settings(
            isolated_hermes_home,
            UpdateSettings(allow_local_ahead=True, safe_mode_auto=False),
        )

        refusal = evaluate_update_force_requirement(repo, home=isolated_hermes_home)
        assert refusal is None, (
            f"Expected None (proceed) when toggle is armed, got {refusal}"
        )

    def test_force_pushed_never_bypassed_by_toggle(
        self, isolated_hermes_home: Path, hermetic_git_repo
    ) -> None:
        """Force-pushed upstream is NEVER bypassed by the toggle. A user-armed
        ``allow_local_ahead`` does not entitle them to ignore a server-side
        history rewrite."""
        repo = hermetic_git_repo.repo
        remote = hermetic_git_repo.remote
        self._make_force_pushed(repo, remote)

        # User arms the toggle (irrelevant to force-pushed, but tests the path).
        write_update_settings(
            isolated_hermes_home,
            UpdateSettings(allow_local_ahead=True, safe_mode_auto=False),
        )

        refusal = evaluate_update_force_requirement(repo, home=isolated_hermes_home)
        assert refusal is FORCE_PUSHED_REFUSAL, (
            f"Expected FORCE_PUSHED_REFUSAL even with toggle armed, got {refusal}"
        )

    def test_clean_state_returns_none_regardless_of_toggle(
        self, isolated_hermes_home: Path, hermetic_git_repo
    ) -> None:
        """No local commits, no force-push → update proceeds."""
        repo = hermetic_git_repo.repo
        # Toggle state shouldn't matter when there is nothing to gate on.
        write_update_settings(
            isolated_hermes_home,
            UpdateSettings(allow_local_ahead=False, safe_mode_auto=False),
        )
        assert evaluate_update_force_requirement(repo, home=isolated_hermes_home) is None

    def test_falls_back_to_project_root_parent_when_home_missing(
        self, tmp_path: Path, hermetic_git_repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the caller omits ``home``, the gate falls back to
        ``project_root.parent``. This is best-effort: the orchestrator always
        passes home explicitly, but the legacy call shape (only ``project_root``)
        must still work.

        Pin ``HERMES_HOME`` to a tmp directory so the origin-SHA cache lives
        under that tmp path (the default ``~/.hermes/update/last_origin_sha``
        would leak state from prior runs on the dev box and trigger a false
        ``force-pushed`` reading).
        """
        home = tmp_path / "isolated-home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        # Place the settings file at project_root.parent. The hermetic_git_repo
        # fixture puts repo at tmp_path2/repo, so tmp_path2 is the parent.
        repo_parent = hermetic_git_repo.repo.parent
        write_update_settings(
            repo_parent,
            UpdateSettings(allow_local_ahead=True),
        )
        # Local ahead
        repo = hermetic_git_repo.repo
        self._make_local_ahead(repo)

        # No home passed; the function should walk to project_root.parent.
        refusal = evaluate_update_force_requirement(repo)
        assert refusal is None, (
            f"Expected the parent-directory fallback to honor the toggle, got {refusal}"
        )