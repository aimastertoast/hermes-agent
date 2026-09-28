"""Shared fixtures for the hermes update test suite.

HARD isolation rules (see spec Section 5.1):
- HERMES_HOME is monkeypatched per test
- No network, no real git against user's clone
- Secrets are obviously fake
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def isolated_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HERMES_HOME isolated to tmp_path. Creates logs/, seed state.db, seed config.yaml."""
    home = tmp_path / "fake-home"
    (home / "logs").mkdir(parents=True)
    (home / "state.db").write_bytes(b"")  # empty placeholder
    (home / "config.yaml").write_bytes(
        b"version: 1\nprofile: default\nmcp_servers: {}\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def hermetic_git_repo(tmp_path: Path) -> "GitRepo":
    """A hermetic git repo + bare remote. Never touches the user's clone."""
    repo_path = tmp_path / "repo"
    remote_path = tmp_path / "remote.git"
    subprocess.check_call(["git", "init", "--bare", str(remote_path)])
    # Force the bare remote's HEAD to refs/heads/main so a fresh clone uses
    # `main` as the default branch on hosts whose `init.defaultBranch` is
    # still `master` (this machine defaults to master).
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
    return GitRepo(repo=repo_path, remote=remote_path)


class GitRepo:
    def __init__(self, repo: Path, remote: Path):
        self.repo = repo
        self.remote = remote


class FakeGatewayProcess:
    """A real subprocess listening on a free port, answering /api/status."""

    def __init__(self, port: int, simulate_start_failure: bool = False):
        self.port = port
        self._start_failure = simulate_start_failure
        self._simulate_health_timeout_s = 0
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._healthy = True

    @classmethod
    def start(cls, *, simulate_start_failure: bool = False) -> "FakeGatewayProcess":
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        gw = cls(port, simulate_start_failure=simulate_start_failure)
        gw._serve()
        return gw

    def _serve(self) -> None:
        if self._start_failure:
            return  # never starts

        gw = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if not gw._healthy:
                    self.send_response(503); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true, "version": "vtest"}')

            def log_message(self, format, *args):  # silence
                pass

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def simulate_health_timeout(self, seconds: int) -> None:
        self._simulate_health_timeout_s = seconds
        self._healthy = False

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def fake_gateway() -> FakeGatewayProcess:
    gw = FakeGatewayProcess.start()
    yield gw
    gw.stop()


@pytest.fixture
def fake_config_yaml() -> bytes:
    return b"version: 1\nprofile: default\nmcp_servers: {}\n"