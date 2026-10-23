"""A Desktop-spawned command must leave an owed source-update tail to ``hermes update``.

The tail holds the update lock, and the lock's live marker fences the gateway
(``update_paused``). A Desktop command that ran the tail inline fenced the very gateway it
was booting, so the Desktop could not start at all -- the repair meant to fix the install
was the thing that broke it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes_cli import venv_sync
from hermes_cli.update_desktop_tail import desktop_owes_tail


@pytest.mark.parametrize(
    "command",
    [
        ["gateway", "ensure"],
        ["gateway", "ensure", "--json"],
        ["gateway", "restart"],
        ["gateway", "stop", "--all"],
        ["config", "get", "updates.pre_update_backup", "--json"],
        ["hermes_cli.desktop_update_verify"],
        ["hermes_cli.update_verification"],
        ["hermes_cli.source_check"],
        ["hermes_cli.source_check_fork"],
        ["hermes_cli.backup_sqlite"],
    ],
)
def test_desktop_commands_own_the_tail(command: list[str]) -> None:
    assert desktop_owes_tail(command) is True


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["update"],
        ["chat"],
        ["pm", "repair"],
        ["gateway", "status"],
        ["config", "set"],
        ["desktop"],
    ],
)
def test_other_commands_keep_the_tail(command: list[str]) -> None:
    assert desktop_owes_tail(command) is False


def _owing_checkout(tmp_path: Path, monkeypatch) -> Path:
    """A checkout whose dependencies are current and whose completion tail is still owed."""
    import hermes_cli._launchers
    import pm
    import pm.environments

    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    (root / "install-stamp.json").write_text('{"updateMechanism": "self"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: True)
    monkeypatch.setattr(pm.environments, "owning_home_root", lambda _root: None)
    monkeypatch.setattr(hermes_cli._launchers, "resolve_store_python", lambda _root: Path(sys.executable))
    monkeypatch.setattr(venv_sync, "_tree_matches_completed_stamp", lambda _root: False)
    venv_sync.arm_completion(root)
    return root


def test_desktop_backend_boot_leaves_the_tail_owed(tmp_path, monkeypatch, capsys) -> None:
    """The Desktop's backend boot must not take the update lock or run the tail."""
    root = _owing_checkout(tmp_path, monkeypatch)
    ran: list[tuple] = []
    monkeypatch.setattr(venv_sync, "_finish_source_update", lambda *a, **k: ran.append(a))

    assert venv_sync.prepare_launch(root, ["gateway", "ensure", "--json"]) is None

    assert ran == [], "the Desktop's backend boot ran the completion tail inline"
    assert "a source update is unfinished" in capsys.readouterr().err
    assert venv_sync.completion_pending_path(root).is_file(), "the tail was dropped instead of owed"


def test_the_desktops_pre_update_snapshot_leaves_the_tail_owed(tmp_path, monkeypatch) -> None:
    """The state.db pre-flight runs the tail with a 30s timeout: an inline tail cancels
    the update it was supposed to protect."""
    root = _owing_checkout(tmp_path, monkeypatch)
    ran: list[tuple] = []
    monkeypatch.setattr(venv_sync, "_finish_source_update", lambda *a, **k: ran.append(a))

    venv_sync.prepare_launch(root, ["--run-module", "hermes_cli.backup_sqlite", str(tmp_path / "home")])

    assert ran == [], "the pre-update snapshot ran the completion tail inline"


def test_the_desktops_pre_update_gateway_stop_leaves_the_tail_owed(tmp_path, monkeypatch) -> None:
    """``gateway stop --all`` runs before the update: an inline tail there holds the
    update lock the update itself is about to take."""
    root = _owing_checkout(tmp_path, monkeypatch)
    ran: list[tuple] = []
    monkeypatch.setattr(venv_sync, "_finish_source_update", lambda *a, **k: ran.append(a))

    venv_sync.prepare_launch(root, ["gateway", "stop", "--all"])

    assert ran == [], "the pre-update gateway stop ran the completion tail inline"


def test_an_ordinary_command_still_runs_the_tail(tmp_path, monkeypatch) -> None:
    """The control: a command the user ran in a shell still completes the owed tail."""
    root = _owing_checkout(tmp_path, monkeypatch)
    ran: list[tuple] = []
    monkeypatch.setattr(venv_sync, "_finish_source_update", lambda *a, **k: ran.append(a))

    venv_sync.prepare_launch(root, ["chat"])

    assert ran, "the completion tail stopped running for ordinary launches"
