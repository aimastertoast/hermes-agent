"""The fork's dashboard update routes — FORK-OWNED.

``hermes_cli/web_routers/actions.py`` is one of the files upstream rewrites most, and
the fork's changes to it were edits to upstream's OWN lines — the shape a routine
upstream merge collides with. A collision ABORTS ``hermes update`` (the Desktop's
"Update failed (exit 1)" banner), so the fork's behavior moved to
``hermes_cli/web_routers/actions_fork.py`` and upstream's file is byte-identical to
upstream again.

These tests pin the behavior the Desktop depends on, through the HTTP surface it
actually calls:

1. ``POST /api/hermes/update`` refuses with the fork's OWN code when the install is
   ahead of upstream, and never spawns the update.
2. The same route defers to upstream's handler when the gate passes.
3. An upstream admission refusal (docker/apt) still wins over the fork's gate.
4. ``GET /api/hermes/update/receipt`` serves the newest COMPLETED receipt from BOTH
   stores — the fork's pipeline record and upstream's legacy archive.
5. ``POST /api/hermes/update/receipt/{id}/ack`` flips the receipt's own flag.
6. ``GET /api/hermes/update/check`` runs with the fork's branch comparison.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from pathlib import Path

import pytest

from hermes_cli import source_check_fork
from hermes_cli import update_contract
from hermes_cli.update_receipt_fork import (
    UpdateReceiptRecord,
    acknowledge_update_receipt,
    read_latest_pipeline_receipt,
    write_pipeline_receipt,
)
from hermes_cli.web_routers import actions


@pytest.fixture()
def client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """The HERMES_HOME the fork's routes read: a temp dir, and no live install."""
    import hermes_constants

    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: root)
    return root


@pytest.fixture()
def no_side_effects(monkeypatch):
    """Keep the update route away from the real install: no commit build, no
    externally-managed dashboard, no admission refusal, no refusal receipt, and a
    spawn that fails the test instead of starting a real update."""
    monkeypatch.setattr(actions, "is_commit_build", lambda _root: False)
    monkeypatch.setattr(actions, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr(actions, "_record_completed_action", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(update_contract, "evaluate_update_admission", lambda _root: None)
    monkeypatch.setattr(update_contract, "record_refusal_receipt", lambda _refusal: None)

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("the update route must not spawn an update")

    monkeypatch.setattr(actions, "_spawn_hermes_action", _no_spawn)


def _pipeline_receipt(home: Path) -> UpdateReceiptRecord:
    """A COMPLETED pipeline receipt (the fork's own store)."""
    record = UpdateReceiptRecord(
        receipt_id=secrets.token_hex(8),
        outcome="success",
        error=None,
        rolled_back=False,
        started_at=1_756_000_000.0,
        finished_at=1_756_000_200.0,
    )
    write_pipeline_receipt(home, record)
    return record


def _legacy_receipt(home: Path, *, outcome: str = "success") -> dict:
    """A COMPLETED receipt in upstream's legacy ``logs/update_receipts/`` archive.

    The archive's file name carries the run's start stamp and update id — the
    reader orders the archive by that name, so the name has to have upstream's
    shape.
    """
    receipt = {
        "schema": 1,
        "started_at": "2026-08-23T07:00:00+00:00",
        "finished_at": "2026-08-23T07:03:20+00:00",
        "argv": ["hermes", "update"],
        "pid": 12345,
        "outcome": outcome,
        "update_id": "c" * 32,
        "action_id": "d" * 32,
        "pre_update": {"sha": "a" * 40, "version": "0.20.4"},
        "post_update": {"sha": "b" * 40, "version": "0.20.5"},
        "steps": [],
        "skips": [],
        "gateway_restart": {},
        "fleet": [{"profile": "default", "pid": 1, "code_sha": "b" * 40, "state": "current"}],
    }
    directory = home / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"update_20260823_070320_12345_{'c' * 32}.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    return receipt


class TestUpdateRouteForceGate:
    """``POST /api/hermes/update`` — the fork's force-requirement gate."""

    def test_refuses_with_the_fork_code_and_never_spawns(self, client, no_side_effects, monkeypatch):
        monkeypatch.setattr(
            update_contract, "evaluate_update_force_requirement",
            lambda _root, **_kwargs: update_contract.LOCAL_AHEAD_REFUSAL,
        )

        resp = client.post("/api/hermes/update")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        # The fork's own code, not upstream's generic "update_not_in_place":
        # the Desktop reports the code next to the message.
        assert body["error"] == update_contract.LOCAL_AHEAD_REFUSAL.code
        assert body["message"] == update_contract.LOCAL_AHEAD_REFUSAL.message
        assert body["name"] == "hermes-update"
        assert body["pid"] is None

    def test_defers_to_upstream_when_the_gate_passes(self, client, no_side_effects, monkeypatch):
        monkeypatch.setattr(
            update_contract, "evaluate_update_force_requirement", lambda _root, **_kwargs: None
        )
        seen: list[bool] = []

        async def _fake_update():
            seen.append(True)
            return {"ok": True, "pid": 42, "name": "hermes-update"}

        monkeypatch.setattr(actions, "update_hermes", _fake_update)

        resp = client.post("/api/hermes/update")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "pid": 42, "name": "hermes-update"}
        assert seen == [True]

    def test_an_admission_refusal_still_wins_over_the_force_gate(self, client, no_side_effects, monkeypatch):
        """A docker/apt install is refused by upstream's admission gate, and that
        refusal must not be masked by the fork's gate."""
        refusal = update_contract.UpdateRefusal("docker", "nope", "docker pull")
        recorded: list[object] = []
        monkeypatch.setattr(update_contract, "evaluate_update_admission", lambda _root: refusal)
        monkeypatch.setattr(
            update_contract, "evaluate_update_force_requirement",
            lambda _root, **_kwargs: update_contract.LOCAL_AHEAD_REFUSAL,
        )
        monkeypatch.setattr(update_contract, "record_refusal_receipt", recorded.append)

        resp = client.post("/api/hermes/update")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        # The admission refusal is the one that was reported.
        assert body["error"] == "docker_update_unsupported"
        assert recorded == [refusal]


class TestReceiptRoute:
    """``GET /api/hermes/update/receipt`` — the fork's dual-store read."""

    def test_serves_the_pipeline_store(self, client, home):
        record = _pipeline_receipt(home)

        resp = client.get("/api/hermes/update/receipt")

        assert resp.status_code == 200
        body = resp.json()
        assert body["receipt"]["receipt_id"] == record.receipt_id
        assert body["receipt"]["outcome"] == "success"
        assert body["summary"]["outcome"] == "success"

    def test_serves_the_legacy_archive(self, client, home):
        _legacy_receipt(home)

        resp = client.get("/api/hermes/update/receipt")

        assert resp.status_code == 200
        body = resp.json()
        assert body["receipt"]["outcome"] == "success"
        assert body["summary"]["post_sha"] == "b" * 40

    def test_404_when_no_receipt_exists(self, client, home):
        resp = client.get("/api/hermes/update/receipt")

        assert resp.status_code == 404


class TestAcknowledgeRoute:
    """``POST /api/hermes/update/receipt/{id}/ack`` — the fork's own route."""

    def test_acknowledges_the_receipt(self, client, home):
        record = _pipeline_receipt(home)

        resp = client.post(f"/api/hermes/update/receipt/{record.receipt_id}/ack")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        stored = read_latest_pipeline_receipt(home)
        assert stored is not None
        assert stored.acknowledged is True

    def test_unknown_id_is_refused(self, client, home):
        resp = client.post("/api/hermes/update/receipt/not-a-receipt/ack")

        assert resp.status_code == 200
        assert resp.json() == {"ok": False}

    def test_the_fork_reader_guards_the_id(self, home):
        """The id guard is the reader's, and it must not walk out of the store."""
        assert acknowledge_update_receipt(home, "../../etc/passwd") is False


class TestCheckRoute:
    """``GET /api/hermes/update/check`` — the fork's branch comparison."""

    def test_runs_with_the_fork_branch_check(self, client, monkeypatch):
        swapped: list[bool] = []

        @contextlib.contextmanager
        def _spy():
            swapped.append(True)
            yield

        monkeypatch.setattr(source_check_fork, "fork_branch_check", _spy)
        called: list[dict] = []

        async def _fake_check(force: bool = False, profile=None):
            called.append({"force": force, "profile": profile})
            return {"behind": 3}

        monkeypatch.setattr(actions, "check_hermes_update", _fake_check)

        resp = client.get("/api/hermes/update/check?force=true")

        assert resp.status_code == 200
        assert resp.json() == {"behind": 3}
        assert swapped == [True]
        assert called == [{"force": True, "profile": None}]
