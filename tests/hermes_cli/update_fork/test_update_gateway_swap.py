import os
import signal
import socket
import subprocess
import sys

import pytest

from hermes_cli.update_gateway_swap import swap_gateway, GatewaySwapResult


# Source for the fake "new gateway" Python script. It listens on a port we
# tell it to, writes that port to a file we name, and answers /api/status
# with 200 — just enough for swap_gateway's health probe to pass. It stays
# alive until killed so we can verify it doesn't die as a side effect of the
# swap.
_FAKE_GATEWAY_SCRIPT = r'''
import http.server
import socketserver
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, format, *args):
        pass


def main():
    port = int(sys.argv[1])
    port_file = sys.argv[2]
    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
    with open(port_file, "w") as f:
        f.write(str(port))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
'''


def _kill_tree(pid: int) -> None:
    """Kill ``pid`` and every descendant. Never raises.

    ``swap_gateway`` spawns the new gateway with a plain ``Popen`` and, by design,
    leaves it running — that is the whole point of a swap. On Windows the venv
    launcher re-execs into the hermes runtime, so the pid ``Popen`` hands back is
    only the *parent* of the process that actually holds the socket and the venv's
    package files. Killing just that pid strands the child, which then outlives the
    test session and keeps the environment locked (this is what made
    ``charset_normalizer\\cd.cp314-win_amd64.pyd`` access-denied and silently
    skipped ``hermes pm repair``'s cleanup step).
    """
    if not pid or pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return
    # POSIX: kill the whole process group so children go with the parent.
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.fixture
def spawned_gateways():
    """Reap every gateway pid a test causes to be spawned, at test teardown.

    Yields a ``track`` callable; each tracked pid is killed (with its descendants)
    during teardown, newest first, regardless of how the test ends.
    """
    tracked: list[int] = []

    def track(pid) -> None:
        if pid:
            tracked.append(int(pid))

    yield track

    for pid in reversed(tracked):
        _kill_tree(pid)


def _spawn_dummy_gateway() -> subprocess.Popen:
    """Spawn a long-running dummy process the test can kill."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_swap_gateway_kills_old_pid(tmp_path, spawned_gateways):
    # Arrange: logs dir + gateway.pid + a fake new-gateway that will
    # become healthy via the health probe.
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    old = _spawn_dummy_gateway()
    (tmp_path / "gateway.pid").write_text(str(old.pid))

    port = _free_port()
    port_file = tmp_path / "gateway.port"
    fake_script = tmp_path / "fake_new_gateway.py"
    fake_script.write_text(_FAKE_GATEWAY_SCRIPT)

    try:
        result = swap_gateway(
            tmp_path,
            health_timeout_s=10,
            override_executable=[
                sys.executable, str(fake_script), str(port), str(port_file),
            ],
        )
        assert isinstance(result, GatewaySwapResult)
        assert result.ok is True, f"expected ok=True, got {result}"
        assert result.old_pid == old.pid
        assert result.new_pid is not None and result.new_pid > 0
        # Old is dead
        assert old.poll() is not None
        # The new gateway outlives the swap on purpose, so the test has to reap
        # it -- otherwise it survives the session holding this venv open.
        spawned_gateways(result.new_pid)
    finally:
        if old.poll() is None:
            old.kill()


def test_swap_gateway_health_timeout_leaves_old(tmp_path):
    old = _spawn_dummy_gateway()
    try:
        # override_executable=None tells swap_gateway to skip the spawn
        # entirely — simulates "new failed to start" without touching
        # the live install or PATH.
        result = swap_gateway(
            tmp_path, health_timeout_s=5, override_executable=None,
        )
        # Either ok=False with new-failed-to-start, or partial
        assert result.ok is False or result.warning
        # Old is still alive
        assert old.poll() is None
    finally:
        old.kill()
