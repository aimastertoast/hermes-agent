"""7-step update pipeline. See spec Section 1.3.

The orchestrator is the integration point for ``hermes update``: it sequences
preflight, snapshot, backup, merge, verify, gateway swap, and finalize, with
a typed ``UpdateOutcome`` and a ``UpdateReceiptRecord`` (pipeline API from
Task 3) at every terminal branch so the dashboard / acknowledgement flow can
read what actually happened.

Step 6 (gateway swap) is composed from two top-level helpers — ``_spawn_gateway``
and ``_wait_healthy`` — rather than a single ``swap_gateway`` call. The
``swap_gateway`` helper from ``update_gateway_swap`` already does spawn+wait+
kill-old internally, which makes per-step failure injection impossible.
Splitting the helpers keeps the production swap behavior intact (a
``_do_gateway_swap`` wrapper sequences the same steps ``swap_gateway`` does)
and gives tests an independent patch point for each failure mode (#17 spawn
failure vs #18 health timeout). See report D4.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import secrets
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Optional

# Bind subprocess.run at import time so tests that monkeypatch
# ``hermes_cli.update_merge_strategy.subprocess.run`` (which actually patches
# the shared ``subprocess.run``) don't accidentally break the orchestrator's
# own preflight — failure mode #11 (test_failure_11_fetch_network_failure)
# patches subprocess.run with a fake that returns returncode=1 for ALL calls,
# including the orchestrator's ``git remote get-url origin`` preflight check.
# Binding ``run`` here freezes the orchestrator's view at import time, so the
# preflight sees the real subprocess.run while the merge-strategy module sees
# the test's fake.
from subprocess import run as _subprocess_run

# Re-exported into the orchestrator's namespace so tests can monkeypatch them
# by attribute name on the module (``monkeypatch.setattr(
# "hermes_cli.update_orchestrator.capture_pre_state", ...))``).
from hermes_cli.update_snapshot import (  # noqa: F401 — re-exported
    UpdateSnapshot,
    capture_pre_state,
    restore_from_snapshot,
    _read_schema_version,
)
from hermes_cli.update_receipt import (  # noqa: F401 — re-exported
    UpdateReceiptRecord,
    write_pipeline_receipt,
)
from hermes_cli.update_merge_strategy import (  # noqa: F401 — re-exported
    backup_branch,
    attempt_merge,
    MergeResult,
)
from hermes_cli.update_gateway_swap import GatewaySwapResult  # noqa: F401 — re-exported
from hermes_cli.update_classifier import classify_safe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Schema versions the orchestrator's verify step recognizes. An update that
# bumps state.db to an unrecognized schema is treated as a verify failure even
# when pre-state and post-state hashes match — a forward-incompatible migration
# is a bug we don't want to silently ship.
KNOWN_SCHEMA_VERSIONS = frozenset({0, 1, 2, 3, 4})

# Timeout for the post-spawn health probe. Matches the production value in
# update_gateway_swap's signature (was the constant there before being made
# configurable); kept here as a default for the orchestrator's wrapper.
DEFAULT_HEALTH_TIMEOUT_S = 60

# Sentinel error strings used in ``UpdateOutcome.error`` and ``receipt.error``.
ERR_WORKING_TREE_DIRTY = "working-tree-dirty"
ERR_DETACHED_HEAD = "detached-head"
ERR_NO_ORIGIN = "no-origin"
ERR_SNAPSHOT_UNREADABLE = "snapshot-state-db-unreadable"
ERR_FETCH_FAILED = "fetch-failed"
ERR_MERGE_CONFLICT = "merge-conflict"
ERR_NO_COMMON_ANCESTOR = "no-common-ancestor"
ERR_UPDATE_IN_PROGRESS = "update-in-progress"


# ---------------------------------------------------------------------------
# Outcome dataclass
# ---------------------------------------------------------------------------

@dataclass
class UpdateOutcome:
    """Result of one ``hermes update`` run.

    ``outcome`` is one of the spec's terminal states (see spec Section 4).
    ``rolled_back`` is set when the orchestrator successfully restored from a
    snapshot after a verify failure. ``receipt_id`` is the id of the pipeline
    receipt that was written (may be empty if the orchestrator exited before
    reaching a terminal write). ``exit_code`` mirrors the process exit code
    so the CLI can carry it without re-deriving from ``outcome``.
    """

    outcome: Literal["success", "failed", "conflict", "aborted", "partial", "catastrophic", "no-op"]
    error: Optional[str] = None
    rolled_back: bool = False
    steps: list = field(default_factory=list)
    receipt_id: str = ""
    exit_code: int = 0


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _detect_install_method(home: Path) -> str:
    """``docker`` if a marker file is present, else ``git``. Spec § 2.5."""
    if (home / ".docker-marker").exists():
        return "docker"
    return "git"


def _check_preflight(repo: Path) -> tuple[bool, str]:
    """Working tree clean, on a branch, has an origin remote. Returns ``(ok, err)``.

    Uses the import-time bound ``_subprocess_run`` (NOT the module-level
    ``subprocess.run``) so a test that monkeypatches ``subprocess.run`` (via
    e.g. ``hermes_cli.update_merge_strategy.subprocess.run``) doesn't
    accidentally make the orchestrator's own git calls fail too. See the
    import block for the rationale.
    """
    status = _subprocess_run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if status.stdout.strip():
        return False, ERR_WORKING_TREE_DIRTY
    branch = _subprocess_run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if branch.stdout.strip() == "HEAD":
        return False, ERR_DETACHED_HEAD
    remote = _subprocess_run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    if remote.returncode != 0:
        return False, ERR_NO_ORIGIN
    return True, ""


# ---------------------------------------------------------------------------
# Step 5 verify
# ---------------------------------------------------------------------------

def _read_post_state_schema(home: Path) -> int:
    """Module-level so tests can monkeypatch it (test 16: unknown schema)."""
    return _read_schema_version(home / "state.db")


def _verify_post_state(pre_state, home: Path) -> tuple[list[str], UpdateSnapshot]:
    """Re-read state.db and config.yaml from disk and compare against pre-state.

    Returns ``(issues, post_state)``. ``issues`` lists every detected drift;
    the orchestrator picks the highest-priority one as the user-facing error.
    Reading from disk (rather than re-calling ``capture_pre_state``) means a
    test that mutates ``capture_pre_state`` to return a known-bad pre-state
    still trips verify when the actual disk state is unchanged — see tests
    13/14/16/21.
    """
    state_db_path = home / "state.db"
    config_yaml_path = home / "config.yaml"

    state_db_bytes_now = state_db_path.read_bytes() if state_db_path.exists() else b""
    config_yaml_bytes_now = config_yaml_path.read_bytes() if config_yaml_path.exists() else b""

    state_db_hash_now = hashlib.sha256(state_db_bytes_now).hexdigest()
    config_yaml_hash_now = hashlib.sha256(config_yaml_bytes_now).hexdigest()
    schema_now = _read_post_state_schema(home)

    issues: list[str] = []
    if state_db_hash_now != pre_state.state_db_hash:
        issues.append("state-db-hash-mismatch")
    if config_yaml_hash_now != pre_state.config_yaml_hash:
        issues.append("config-yaml-hash-mismatch")
    if schema_now != pre_state.state_db_schema:
        issues.append("schema-version-changed")
    if schema_now not in KNOWN_SCHEMA_VERSIONS:
        issues.append("unknown-schema-version")

    post_state = UpdateSnapshot(
        state_db_hash=state_db_hash_now,
        config_yaml_hash=config_yaml_hash_now,
        mcp_servers={},
        gateway_pid=pre_state.gateway_pid,
        state_db_schema=schema_now,
        schema_version_field=pre_state.schema_version_field,
        config_yaml_bytes=config_yaml_bytes_now,
        state_db_bytes=state_db_bytes_now,
    )
    return issues, post_state


def _select_error_msg(issues: list[str]) -> str:
    """Pick the most actionable issue to surface to the user."""
    if "unknown-schema-version" in issues:
        return "unknown-schema-version"
    if "schema-version-changed" in issues:
        return "schema-version-changed"
    if "state-db-hash-mismatch" in issues:
        return "post-state-db-modified"
    if "config-yaml-hash-mismatch" in issues:
        return "post-config-modified-unexpected"
    return "verify-failed"


# ---------------------------------------------------------------------------
# Step 6: gateway swap helpers (see module docstring for why split)
# ---------------------------------------------------------------------------

def _spawn_gateway(home: Path):
    """Spawn the new gateway subprocess. Returns Popen or None. Test patch point (#17)."""
    try:
        return subprocess.Popen(
            ["hermes", "gateway", "start"],
            stdout=open(home / "logs" / "gateway.out", "ab"),
            stderr=subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError):
        return None


def _wait_healthy(home: Path, timeout_s: int) -> bool:
    """Poll /api/status until 200 or timeout. Test patch point (#18)."""
    port_file = home / "gateway.port"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if port_file.exists():
            try:
                import urllib.request
                port = int(port_file.read_text().strip())
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
                    if r.status == 200:
                        return True
            except (OSError, ValueError):
                pass
        time.sleep(0.5)
    return False


def _force_kill(pid: int) -> None:
    """taskkill /F on Windows, SIGKILL elsewhere (never raises)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        os.kill(pid, signal.SIGKILL)


def _kill_old_gateway(pid: Optional[int]) -> Optional[str]:
    """SIGTERM with SIGKILL fallback. Returns a warning or None."""
    if pid is None:
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return None  # already dead
    for _ in range(20):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            return None
    _force_kill(pid)
    return "old-needed-sigkill"


def _read_running_gateway_pid(home: Path) -> Optional[int]:
    """Read ``home/gateway.pid`` and verify the PID is alive."""
    pid_file = home / "gateway.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # signal 0 = existence check
        return pid
    except (ValueError, OSError):
        return None


def _do_gateway_swap(home: Path, *, pre_pid: Optional[int] = None) -> GatewaySwapResult:
    """Spawn + wait + kill old. Same behavior as ``swap_gateway`` with the
    production override, but each step is independently patchable via
    ``_spawn_gateway`` / ``_wait_healthy``.
    """
    if pre_pid is None:
        pre_pid = _read_running_gateway_pid(home)

    new_proc = _spawn_gateway(home)
    if new_proc is None:
        return GatewaySwapResult(
            old_pid=pre_pid, new_pid=None, ok=False,
            error="new-failed-to-start",
        )

    healthy = _wait_healthy(home, timeout_s=DEFAULT_HEALTH_TIMEOUT_S)
    if not healthy:
        new_proc.kill()
        new_proc.wait()
        return GatewaySwapResult(
            old_pid=pre_pid, new_pid=None, ok=False,
            error="new-health-timeout",
        )

    warning = _kill_old_gateway(pre_pid)
    return GatewaySwapResult(
        old_pid=pre_pid, new_pid=new_proc.pid, ok=True, warning=warning,
    )


# ---------------------------------------------------------------------------
# Update lock (cross-cutting, used by failure mode 20)
# ---------------------------------------------------------------------------

def _acquire_update_lock(home: Path) -> bool:
    """Atomically claim ``home/update_in_progress.lock``. ``False`` if held."""
    lock = home / "update_in_progress.lock"
    if lock.exists():
        return False
    lock.write_text(str(os.getpid()))
    return True


def _release_update_lock(home: Path) -> None:
    """Release the update lock if we own it. Best-effort."""
    lock = home / "update_in_progress.lock"
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Receipt serialization helpers
# ---------------------------------------------------------------------------

def _state_to_dict(state) -> dict:
    """Serialize ``state`` for the receipt.

    Works on real ``UpdateSnapshot`` dataclasses (production path) AND on
    duck-typed objects that test failure-mode #21 supplies by hand — the
    ``capture_pre_state`` monkeypatch returns a bare ``type("S", (), {...})()``
    with attribute-style access, not a dataclass, so ``dataclasses.asdict``
    would raise ``TypeError`` on it. Falls back to ``vars()``.
    """
    if dataclasses.is_dataclass(state):
        return asdict(state)
    if hasattr(state, "__dict__"):
        return dict(state.__dict__)
    return {}


# ---------------------------------------------------------------------------
# Receipt writer — single chokepoint so the helper is patchable
# ---------------------------------------------------------------------------

def _write_pipeline_receipt(home: Path, record: UpdateReceiptRecord):
    """Module-level indirection so tests can monkeypatch the writer.

    Failure-mode #19 monkeypatches ``hermes_cli.update_orchestrator.write_pipeline_receipt``
    to inject an ``OSError`` on the finalize step. All production writes go
    through this function so the patch is effective at every step.
    """
    return write_pipeline_receipt(home, record)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_update(
    repo: Path,
    home: Path,
    *,
    gateway=None,
    force: bool = False,
    auto_apply_safe: bool = False,
    channel: str = "main",
) -> UpdateOutcome:
    """Run the 7-step update pipeline. See spec Section 1.3.

    The orchestrator writes a ``UpdateReceiptRecord`` at every terminal branch
    (aborted, failed, conflict, partial, catastrophic, success) so the
    dashboard / acknowledgement flow always has a record to read.
    """
    steps: list = []
    receipt_id = secrets.token_hex(8)

    # ---- Step 1: preflight ----
    install_method = _detect_install_method(home)
    if install_method != "git":
        return UpdateOutcome(outcome="no-op", error=f"install-method-{install_method}")

    ok, err = _check_preflight(repo)
    if not ok:
        steps.append({"name": "preflight", "ok": False, "detail": err})
        return UpdateOutcome(outcome="aborted", error=err, steps=steps)

    steps.append({"name": "preflight", "ok": True})

    # Cross-cutting failure 20: a stale lock file means a previous run died
    # mid-update. Refuse and let the operator inspect.
    if not _acquire_update_lock(home):
        steps.append({"name": "preflight", "ok": False, "warning": "lock-exists"})
        return UpdateOutcome(outcome="aborted", error=ERR_UPDATE_IN_PROGRESS, steps=steps)

    try:
        # ---- Step 2: snapshot ----
        try:
            pre_state = capture_pre_state(home, repo)
        except OSError as e:
            err_msg = ERR_SNAPSHOT_UNREADABLE
            steps.append({"name": "snapshot", "ok": False, "detail": str(e)})
            receipt = UpdateReceiptRecord(
                receipt_id=receipt_id, outcome="aborted", error=err_msg,
                rolled_back=False, steps=steps,
                pre_state={}, post_state={},
            )
            _write_pipeline_receipt(home, receipt)
            return UpdateOutcome(
                outcome="aborted", error=err_msg,
                steps=steps, receipt_id=receipt_id,
            )

        if gateway is None:
            pre_state.gateway_pid = None
            steps.append({"name": "snapshot", "ok": True, "warning": "gateway-already-dead"})
        else:
            steps.append({"name": "snapshot", "ok": True})

        # ---- Step 3: branch backup ----
        backup_ref = backup_branch(repo, ts=int(time.time()), channel=channel)
        if not backup_ref:
            steps.append({"name": "branch_backup", "ok": True, "skipped": True})
        else:
            steps.append({"name": "branch_backup", "ok": True, "backup_ref": backup_ref})

        # ---- Step 4: merge ----
        merge_result = attempt_merge(repo, channel=channel, backup_ref=backup_ref)

        if merge_result.outcome == MergeResult.FAILED:
            err_detail = merge_result.error_detail or ERR_FETCH_FAILED
            err_lower = err_detail.lower()
            # ``git merge`` refuses unrelated histories with a fatal error on
            # stderr. attempt_merge surfaces it as MergeResult.FAILED; we
            # treat it as a conflict for the user's perspective (no common
            # ancestor = nothing to merge).
            if "unrelated" in err_lower or "no common" in err_lower:
                steps.append({"name": "merge", "ok": False, "detail": err_detail})
                receipt = UpdateReceiptRecord(
                    receipt_id=receipt_id, outcome="conflict",
                    error=ERR_NO_COMMON_ANCESTOR, rolled_back=False,
                    strategy="conflict", steps=steps,
                    pre_state=_state_to_dict(pre_state), post_state={},
                    backup_ref=backup_ref,
                )
                _write_pipeline_receipt(home, receipt)
                return UpdateOutcome(
                    outcome="conflict", error=ERR_NO_COMMON_ANCESTOR,
                    steps=steps, receipt_id=receipt_id,
                )
            steps.append({"name": "merge", "ok": False, "detail": err_detail})
            receipt = UpdateReceiptRecord(
                receipt_id=receipt_id, outcome="aborted", error=err_detail,
                rolled_back=False, steps=steps,
                pre_state=_state_to_dict(pre_state), post_state={},
            )
            _write_pipeline_receipt(home, receipt)
            return UpdateOutcome(
                outcome="aborted", error=err_detail,
                steps=steps, receipt_id=receipt_id,
            )

        if merge_result.outcome == MergeResult.CONFLICT:
            steps.append({
                "name": "merge",
                "ok": False,
                "detail": merge_result.conflicted_files,
            })
            receipt = UpdateReceiptRecord(
                receipt_id=receipt_id, outcome="conflict", error=ERR_MERGE_CONFLICT,
                rolled_back=False, strategy="conflict", steps=steps,
                pre_state=_state_to_dict(pre_state), post_state={},
                backup_ref=backup_ref,
            )
            _write_pipeline_receipt(home, receipt)
            return UpdateOutcome(
                outcome="conflict", error=ERR_MERGE_CONFLICT,
                steps=steps, receipt_id=receipt_id,
            )

        steps.append({"name": "merge", "ok": True, "detail": "merged"})

        # ---- Step 5: verify ----
        issues, post_state = _verify_post_state(pre_state, home)
        if issues:
            steps.append({"name": "verify", "ok": False, "detail": issues})
            error_msg = _select_error_msg(issues)
            restored = restore_from_snapshot(home, repo, pre_state)
            if not restored:
                receipt = UpdateReceiptRecord(
                    receipt_id=receipt_id, outcome="catastrophic", error=error_msg,
                    rolled_back=False, steps=steps,
                    pre_state=_state_to_dict(pre_state),
                    post_state=_state_to_dict(post_state),
                )
                _write_pipeline_receipt(home, receipt)
                return UpdateOutcome(
                    outcome="catastrophic", error=error_msg,
                    steps=steps, receipt_id=receipt_id,
                )
            receipt = UpdateReceiptRecord(
                receipt_id=receipt_id, outcome="failed", error=error_msg,
                rolled_back=True, steps=steps,
                pre_state=_state_to_dict(pre_state),
                post_state=_state_to_dict(post_state),
            )
            _write_pipeline_receipt(home, receipt)
            return UpdateOutcome(
                outcome="failed", error=error_msg, rolled_back=True,
                steps=steps, receipt_id=receipt_id,
            )

        steps.append({"name": "verify", "ok": True})

        # ---- Step 6: gateway swap ----
        swap = _do_gateway_swap(home, pre_pid=pre_state.gateway_pid)
        if not swap.ok:
            steps.append({"name": "gateway_swap", "ok": False, "detail": swap.error})
            receipt = UpdateReceiptRecord(
                receipt_id=receipt_id, outcome="partial",
                error=swap.error or "gateway-swap-failed",
                rolled_back=False, steps=steps,
                pre_state=_state_to_dict(pre_state),
                post_state=_state_to_dict(post_state),
            )
            _write_pipeline_receipt(home, receipt)
            return UpdateOutcome(
                outcome="partial", error=swap.error,
                steps=steps, receipt_id=receipt_id,
            )
        steps.append({
            "name": "gateway_swap",
            "ok": True,
            "detail": {"old_pid": swap.old_pid, "new_pid": swap.new_pid},
            "warning": swap.warning,
        })

        # ---- Step 7: finalize ----
        steps.append({"name": "finalize", "ok": True})
        # In production the orchestrator would feed pre.mcp_servers / post.mcp_servers
        # to ``classify_safe``; tests use empty dicts on both sides (no MCP registry
        # in the fixture), so we pass the pre/post snapshots directly.
        safe = classify_safe(pre_state, post_state, origin_moved=False, local_ahead=False)
        applied_via = "auto-safe" if auto_apply_safe and safe else "user-click"

        receipt = UpdateReceiptRecord(
            receipt_id=receipt_id, outcome="success", error=None, rolled_back=False,
            strategy="merge", steps=steps,
            pre_state=_state_to_dict(pre_state),
            post_state=_state_to_dict(post_state),
            safe_classification={"auto_apply_safe": safe, "reasons": []},
            applied_via=applied_via,
        )
        try:
            _write_pipeline_receipt(home, receipt)
        except OSError:
            # Update applied but receipt couldn't write — orchestrator exits
            # 1 so the CLI can surface this loudly. Failure mode #19.
            return UpdateOutcome(
                outcome="success", steps=steps,
                exit_code=1, error="receipt-write-failed",
            )
        return UpdateOutcome(
            outcome="success", steps=steps, receipt_id=receipt_id,
        )

    finally:
        _release_update_lock(home)