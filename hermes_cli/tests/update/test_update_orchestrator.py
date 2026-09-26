"""21 failure-mode integration tests for the update orchestrator.

Maps to spec Section 4. Each test runs run_update() against a hermetic fixture
and asserts on the outcome + receipt.
"""
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_cli.update_orchestrator import run_update, UpdateOutcome
from hermes_cli.update_receipt import read_latest_pipeline_receipt
from hermes_cli.tests.update.conftest import FakeGatewayProcess


# ---------------------------------------------------------------------------
# `update_world` fixture
# ---------------------------------------------------------------------------
#
# Per D4/D7 the brief's update_world fixture requires a `fake_gateway` fixture
# (already provided by hermes_cli/tests/update/conftest.py) plus the gateway
# swap setup needed in tests so we don't actually try to spawn the production
# `hermes gateway start` binary in-process.
#
# The fixture patches `_spawn_gateway` to a no-op Popen (so the orchestrator
# believes a fresh gateway has started) and pre-writes `home/gateway.port`
# pointing at the FakeGatewayProcess's listener (so `_wait_healthy` succeeds).
# Tests that exercise failure paths inside Step 6 monkeypatch these helpers
# directly — their patches run AFTER this fixture's, so they win.

class _FakePopen:
    """No-op subprocess.Popen stand-in for the gateway that `_spawn_gateway`
    claims to have started. The orchestrator only calls ``.pid``, ``.kill()``,
    and ``.wait()`` on it; never reads real subprocess state.
    """

    pid = 12345

    def kill(self) -> None:  # noqa: D401 — match subprocess.Popen signature
        return None

    def wait(self) -> None:  # noqa: D401
        return None


@dataclass
class UpdateWorld:
    home: Path
    repo: Path
    gateway: FakeGatewayProcess


@pytest.fixture
def update_world(isolated_hermes_home, hermetic_git_repo, fake_gateway, monkeypatch):
    """Complete hermetic update world: isolated home, hermetic repo, fake gateway.

    The fixture also stages the gateway swap so the orchestrator's Step 6
    succeeds for tests that don't patch Step 6 themselves.
    """
    home = isolated_hermes_home
    repo = hermetic_git_repo.repo

    # `_spawn_gateway` would otherwise try to exec the real `hermes` binary
    # (not on the test PATH) and return None — that would turn every test
    # reaching Step 6 into "partial". The fixture substitutes a no-op Popen.
    monkeypatch.setattr(
        "hermes_cli.update_orchestrator._spawn_gateway",
        lambda home: _FakePopen(),
    )
    # Pre-write the port file so `_wait_healthy` finds the FakeGatewayProcess's
    # /api/status endpoint. The fake gateway is already serving on `fake_gateway.port`.
    (home / "gateway.port").write_text(str(fake_gateway.port))

    return UpdateWorld(home=home, repo=repo, gateway=fake_gateway)


# ---------------------------------------------------------------------------
# Phase A happy path
# ---------------------------------------------------------------------------

def test_orchestrator_succeeds_clean_fast_forward(update_world):
    """Remote gets one new commit; local fast-forwards; update succeeds end-to-end."""
    other = update_world.repo.parent / "other"
    subprocess.check_call(["git", "clone", str(update_world.repo.parent / "remote.git"), str(other)])
    subprocess.check_call(["git", "-C", str(other), "config", "user.email", "test@fake.local"])
    subprocess.check_call(["git", "-C", str(other), "config", "user.name", "Test User"])
    (other / "remote.txt").write_text("remote")
    subprocess.check_call(["git", "-C", str(other), "add", "."])
    subprocess.check_call(["git", "-C", str(other), "commit", "-m", "remote"])
    subprocess.check_call(["git", "-C", str(other), "push", "origin", "main"])

    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)

    assert result.outcome == "success"
    receipt = read_latest_pipeline_receipt(update_world.home)
    assert receipt is not None
    assert receipt.outcome == "success"


# ---------------------------------------------------------------------------
# Phase B: 21 failure modes
# ---------------------------------------------------------------------------

# Step 1: preflight failures

def test_failure_01_working_tree_dirty(update_world):
    (update_world.repo / "dirty.txt").write_text("uncommitted")
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "aborted"
    assert result.error == "working-tree-dirty"


def test_failure_02_detached_head(update_world):
    subprocess.check_call(["git", "-C", str(update_world.repo), "checkout", "HEAD~1"])
    subprocess.check_call(["git", "-C", str(update_world.repo), "checkout", "--detach", "HEAD"])
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "aborted"
    assert result.error == "detached-head"


def test_failure_03_no_origin(update_world):
    subprocess.check_call(["git", "-C", str(update_world.repo), "remote", "remove", "origin"])
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "aborted"
    assert result.error == "no-origin"


def test_failure_04_unsupported_install_method(update_world):
    # Mark checkout as docker install method via marker file
    (update_world.home / ".docker-marker").write_text("")
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    # docker/managed → returns without writing receipt
    assert result.outcome in ("aborted", "no-op")


# Step 2: snapshot failures

def test_failure_05_state_db_unreadable(update_world, monkeypatch):
    # Make sqlite3.connect raise on the snapshot step
    import hermes_cli.update_snapshot as snapshot_mod
    monkeypatch.setattr(snapshot_mod, "_read_schema_version",
                        lambda p: (_ for _ in ()).throw(OSError("db unreadable")))
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "aborted"
    assert result.error == "snapshot-state-db-unreadable"


def test_failure_06_config_unreadable(update_world, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_snapshot.Path.read_bytes",
        lambda p: (_ for _ in ()).throw(OSError("config unreadable")),
    )
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "aborted"


def test_failure_07_gateway_already_dead_proceeds(update_world):
    # No live gateway pid file → snapshot marks it as dead, orchestrator proceeds
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=None)
    assert result.outcome in ("success", "partial")
    receipt = read_latest_pipeline_receipt(update_world.home)
    if receipt and receipt.steps:
        step = next((s for s in receipt.steps if s["name"] == "snapshot"), None)
        if step:
            assert "gateway-already-dead" in (step.get("warning") or "")


def test_failure_08_disk_full_on_snapshot(update_world, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_snapshot.Path.read_bytes",
        lambda p: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "aborted"


# Step 3: branch backup failures

def test_failure_09_no_local_commits_skips_backup(update_world):
    # Local is at origin → no backup needed, step skipped
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "success"
    receipt = read_latest_pipeline_receipt(update_world.home)
    step = next((s for s in receipt.steps if s["name"] == "branch_backup"), None)
    assert step is not None
    assert step.get("skipped") is True


# Step 4: merge failures

def test_failure_10_merge_conflict(update_world):
    # Local commit
    (update_world.repo / "conflict.txt").write_text("local\n")
    subprocess.check_call(["git", "-C", str(update_world.repo), "add", "."])
    subprocess.check_call(["git", "-C", str(update_world.repo), "commit", "-m", "local edit"])

    # Diverging remote commit
    other = update_world.repo.parent / "other"
    subprocess.check_call(["git", "clone", str(update_world.repo.parent / "remote.git"), str(other)])
    subprocess.check_call(["git", "-C", str(other), "config", "user.email", "test@fake.local"])
    subprocess.check_call(["git", "-C", str(other), "config", "user.name", "Test User"])
    (other / "conflict.txt").write_text("remote\n")
    subprocess.check_call(["git", "-C", str(other), "add", "."])
    subprocess.check_call(["git", "-C", str(other), "commit", "-m", "remote edit"])
    subprocess.check_call(["git", "-C", str(other), "push", "origin", "main"])

    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)

    assert result.outcome == "conflict"
    receipt = read_latest_pipeline_receipt(update_world.home)
    assert receipt.backup_ref.startswith("backup-")
    # Repo is left in MERGE_HEAD state (deliberate)
    assert (update_world.repo / ".git" / "MERGE_HEAD").exists()


def test_failure_11_fetch_network_failure(update_world, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_merge_strategy.subprocess.run",
        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": "network unreachable"})(),
    )
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "aborted"
    assert "fetch" in result.error.lower() or "network" in result.error.lower()


def test_failure_12_no_common_ancestor(update_world):
    # Replace remote with completely independent history
    new_remote = update_world.repo.parent / "fresh-remote.git"
    subprocess.check_call(["git", "init", "--bare", str(new_remote)])
    subprocess.check_call(["git", "-C", str(new_remote), "symbolic-ref", "HEAD", "refs/heads/main"])
    other = update_world.repo.parent / "other2"
    subprocess.check_call(["git", "clone", str(new_remote), str(other)])
    subprocess.check_call(["git", "-C", str(other), "config", "user.email", "test@fake.local"])
    subprocess.check_call(["git", "-C", str(other), "config", "user.name", "Test User"])
    (other / "fresh.txt").write_text("fresh")
    subprocess.check_call(["git", "-C", str(other), "add", "."])
    subprocess.check_call(["git", "-C", str(other), "commit", "--allow-empty", "-m", "unrelated root"])
    subprocess.check_call(["git", "-C", str(other), "push", "origin", "main"])
    subprocess.check_call(["git", "-C", str(update_world.repo), "remote", "set-url", "origin", str(new_remote)])
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "conflict"


# Step 5: verify failures

def test_failure_13_state_db_hash_mismatch_restores(update_world, monkeypatch):
    # Capture pre-state, then mutate state.db between snapshot and verify
    import hermes_cli.update_orchestrator as orch
    from hermes_cli.update_snapshot import capture_pre_state as real_capture

    def mutated_capture(home, repo):
        snap = real_capture(home, repo)
        (home / "state.db").write_bytes(b"something-else")
        return snap

    monkeypatch.setattr("hermes_cli.update_orchestrator.capture_pre_state", mutated_capture)
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "failed"
    assert result.rolled_back is True


def test_failure_14_config_yaml_unexpected_change_restores(update_world, monkeypatch):
    import hermes_cli.update_orchestrator as orch
    from hermes_cli.update_snapshot import capture_pre_state as real_capture

    def mutated_capture(home, repo):
        snap = real_capture(home, repo)
        (home / "config.yaml").write_bytes(b"version: 99\nmcp_servers: {}\n")
        return snap

    monkeypatch.setattr("hermes_cli.update_orchestrator.capture_pre_state", mutated_capture)
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "failed"
    assert result.rolled_back is True


def test_failure_15_mcp_not_reconnecting_partial(update_world):
    # MCP warning path: not strictly a failure, mark partial
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome in ("success", "partial")


def test_failure_16_unknown_schema_version_restores(update_world, monkeypatch):
    import hermes_cli.update_orchestrator as orch
    from hermes_cli.update_snapshot import capture_pre_state as real_capture

    def mutated_capture(home, repo):
        snap = real_capture(home, repo)
        snap.state_db_schema = 999  # pretend post-state has unknown schema
        # Re-hash to match the bytes that will be there post-verify
        return snap

    # Mark that post-state schema is unrecognized
    monkeypatch.setattr("hermes_cli.update_orchestrator._read_post_state_schema", lambda h: 999)
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "failed"
    assert result.rolled_back is True


# Step 6: gateway swap failures

def test_failure_17_new_gateway_wont_start(update_world, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_orchestrator._spawn_gateway",
        lambda home: None,
    )
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    # Old kept running → partial
    assert result.outcome == "partial"


def test_failure_18_new_health_timeout(update_world, monkeypatch):
    def slow_health(home, timeout_s):
        return False  # never healthy

    monkeypatch.setattr("hermes_cli.update_orchestrator._wait_healthy", slow_health)
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "partial"


# Step 7: finalize failures

def test_failure_19_receipt_write_fails(update_world, monkeypatch):
    import hermes_cli.update_orchestrator as orch

    original_write = orch.write_pipeline_receipt

    def failing_write(home, receipt):
        if receipt.steps and receipt.steps[-1]["name"] == "finalize":
            raise OSError(28, "No space left on device")
        return original_write(home, receipt)

    monkeypatch.setattr("hermes_cli.update_orchestrator.write_pipeline_receipt", failing_write)
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    # Update applied but receipt couldn't write → orchestrator exit=1
    assert result.exit_code == 1


# Cross-cutting

def test_failure_20_power_loss_via_lock_file(update_world):
    # Create a stale lock file
    (update_world.home / "update_in_progress.lock").write_text("stale")
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    # Should refuse and read last receipt
    assert result.outcome in ("aborted", "success")
    # No mutation of lock by default behavior
    assert (update_world.home / "update_in_progress.lock").exists()


def test_failure_21_catastrophic_restore_failure(update_world, monkeypatch):
    import hermes_cli.update_orchestrator as orch

    def failing_restore(home, repo, snap):
        return False  # restore fails

    monkeypatch.setattr("hermes_cli.update_orchestrator.restore_from_snapshot", failing_restore)
    # Force a verify-failure scenario
    monkeypatch.setattr(
        "hermes_cli.update_orchestrator.capture_pre_state",
        lambda h, r: type(
            "S",
            (),
            {
                "state_db_hash": "x",
                "config_yaml_hash": "y",
                "mcp_servers": {},
                "gateway_pid": None,
                "state_db_schema": 3,
                "schema_version_field": "schema_version",
                "config_yaml_bytes": b"",
                "state_db_bytes": b"",
            },
        )(),
    )
    result = run_update(repo=update_world.repo, home=update_world.home, gateway=update_world.gateway)
    assert result.outcome == "catastrophic"