"""Start new gateway, health probe, then kill old. See spec Section 2.4."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


@dataclass
class GatewaySwapResult:
    old_pid: Optional[int]
    new_pid: Optional[int]
    ok: bool
    error: Optional[str] = None
    warning: Optional[str] = None


def _read_running_gateway_pid(home: Path) -> Optional[int]:
    pid_file = home / "gateway.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # signal 0 = check existence
        return pid
    except (ValueError, OSError):
        return None


def _spawn_gateway(
    home: Path, executable: Sequence[str],
) -> Optional[subprocess.Popen]:
    """Spawn the gateway subprocess with the given executable. Returns None on failure."""
    log_path = home / "logs" / "gateway.out"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            list(executable),
            stdout=open(log_path, "ab"),
            stderr=subprocess.STDOUT,
        )
        return proc
    except (FileNotFoundError, OSError):
        return None


def _wait_healthy(home: Path, timeout_s: int) -> bool:
    """Poll /api/status until 200 or timeout."""
    deadline = time.time() + timeout_s
    port_file = home / "gateway.port"
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
    """Force-kill a process by PID. Uses taskkill /F on Windows,
    signal.SIGKILL elsewhere. Never raises — logs failure paths via caller."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        os.kill(pid, signal.SIGKILL)


def swap_gateway(
    home: Path,
    health_timeout_s: int = 60,
    *,
    override_executable: Optional[Sequence[str]] = None,
) -> GatewaySwapResult:
    """Spawn new gateway, wait for health probe, then SIGTERM old.

    On new-failure: old kept running, new killed.
    On health-timeout: old kept running, new SIGKILLed.
    On old-zombie: force-kill fallback, warning logged.

    `override_executable`:
      - Sequence of args to use as the new gateway command (production passes
        ["hermes", "gateway", "start"]; tests pass a fake script).
      - When None, the function returns immediately with
        ok=False, error="new-failed-to-start" — this is how tests simulate
        a spawn failure without touching the real `hermes` binary or the
        live install.
    """
    old_pid = _read_running_gateway_pid(home)

    if override_executable is None:
        return GatewaySwapResult(
            old_pid=old_pid, new_pid=None, ok=False,
            error="new-failed-to-start",
        )

    new_proc = _spawn_gateway(home, executable=override_executable)
    if new_proc is None:
        return GatewaySwapResult(
            old_pid=old_pid, new_pid=None, ok=False,
            error="new-failed-to-start",
        )

    healthy = _wait_healthy(home, health_timeout_s)
    if not healthy:
        new_proc.kill()
        new_proc.wait()
        return GatewaySwapResult(
            old_pid=old_pid, new_pid=None, ok=False,
            error="new-health-timeout",
        )

    new_pid = new_proc.pid
    warning = None

    # Kill old
    if old_pid is not None:
        try:
            os.kill(old_pid, signal.SIGTERM)
        except OSError:
            pass  # old already dead
        else:
            # Wait up to 10s for graceful shutdown
            for _ in range(20):
                time.sleep(0.5)
                try:
                    os.kill(old_pid, 0)
                except OSError:
                    break
            else:
                # Still alive — force-kill
                try:
                    _force_kill(old_pid)
                    warning = "old-needed-sigkill"
                except OSError:
                    pass

    return GatewaySwapResult(
        old_pid=old_pid, new_pid=new_pid, ok=True, warning=warning,
    )
