"""Tests for the two-store receipt reader the Desktop reads — FORK-OWNED.

The Desktop's Update button runs the DEFAULT ``hermes update`` path, which
writes upstream's legacy archive (``logs/update_receipts/update_*.json``) —
NOT the pipeline store the orchestrator writes. A reader that only knows the
pipeline store therefore returns ``None`` for every update the user actually
ran, and the Desktop's receipt pane can never render.

The reader (``read_latest_update_receipt_dict``) and the per-store
acknowledgement (``acknowledge_update_receipt``) live in the fork-owned
``hermes_cli/update_receipt_fork.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.update_receipt_fork import (
    UpdateReceiptRecord,
    acknowledge_update_receipt,
    read_latest_update_receipt_dict,
    write_pipeline_receipt,
)


def _write_legacy_receipt(
    home: Path,
    *,
    update_id: str,
    outcome: str = "failed",
    finished_at: str | None = "2026-10-20T12:07:37.711465+00:00",
    stop_reason: str | None = "sys.exit(1)",
    stamp: str = "20261020_120737",
    pid: int = 4242,
    name: str = "update",
) -> Path:
    """Write one legacy archive file, shaped like upstream's own writer."""
    directory = home / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}_{stamp}_{pid}_{update_id}.json"
    payload = {
        "outcome": outcome,
        "started_at": "2026-10-20T12:07:37.115996+00:00",
        "finished_at": finished_at,
        "update_id": update_id,
        "pid": pid,
        "steps": [{"name": "pre_state_capture", "ok": True, "detail": ""}],
        "skips": [],
        "pre_update": {"sha": "abc123"},
        "post_update": {"sha": "abc123"},
        "fleet": [],
    }
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLegacyStoreReceipt:
    """The legacy archive is what the Desktop's Update button actually
    writes. Reading it is the difference between a receipt pane that shows
    and one that never renders."""

    def test_legacy_receipt_is_returned_when_pipeline_store_is_empty(
        self, isolated_hermes_home
    ):
        """The decisive case: no pipeline receipt, but the default update
        path wrote a legacy receipt. The reader must return it."""
        home = isolated_hermes_home
        update_id = "a" * 32
        _write_legacy_receipt(home, update_id=update_id)

        payload = read_latest_update_receipt_dict(home)

        assert payload is not None
        assert payload["outcome"] == "failed"
        # The desktop keys the receipt by `receipt_id`; the legacy store
        # calls it `update_id`.
        assert payload["receipt_id"] == update_id
        # The desktop needs a per-receipt acknowledged flag.
        assert payload["acknowledged"] is False
        # ...and an error string to show (the legacy store has no `error`
        # key — the failure detail lives in `stop_reason`).
        assert payload["error"] == "sys.exit(1)"
        # Upstream's own keys survive untouched: the dashboard reads the
        # same receipt and expects upstream's shape.
        assert payload["update_id"] == update_id
        assert payload["steps"][0]["name"] == "pre_state_capture"

    def test_unfinished_runs_are_skipped(self, isolated_hermes_home):
        """A run that is still going (or was killed) has no `finished_at`
        and no outcome to report — the newest COMPLETED receipt wins."""
        home = isolated_hermes_home
        _write_legacy_receipt(
            home,
            update_id="b" * 32,
            outcome="failed",
            finished_at="2026-10-19T12:07:37.711465+00:00",
            stamp="20261019_120737",
        )
        # Newer, but never finished.
        _write_legacy_receipt(
            home,
            update_id="c" * 32,
            outcome="running",
            finished_at=None,
            stamp="20261020_130000",
        )

        payload = read_latest_update_receipt_dict(home)

        assert payload is not None
        assert payload["receipt_id"] == "b" * 32

    def test_non_update_receipts_are_ignored(self, isolated_hermes_home):
        """Plugin-check / sync receipts share the store. They are not update
        runs and must never be mistaken for one."""
        home = isolated_hermes_home
        directory = home / "logs" / "update_receipts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "pm_20261020T130000Z-plugin-check-1-deadbeef.json").write_text(
            json.dumps(
                {"outcome": "success", "finished_at": "2026-10-20T13:00:00+00:00"}
            ),
            encoding="utf-8",
        )

        assert read_latest_update_receipt_dict(home) is None

    def test_newest_completed_receipt_wins(self, isolated_hermes_home):
        home = isolated_hermes_home
        _write_legacy_receipt(
            home,
            update_id="d" * 32,
            outcome="failed",
            finished_at="2026-10-19T12:07:37.711465+00:00",
            stamp="20261019_120737",
        )
        _write_legacy_receipt(
            home,
            update_id="e" * 32,
            outcome="failed",
            finished_at="2026-10-20T12:07:37.711465+00:00",
            stamp="20261020_120737",
        )

        payload = read_latest_update_receipt_dict(home)

        assert payload is not None
        assert payload["receipt_id"] == "e" * 32

    def test_pipeline_receipt_wins_when_it_is_newer(self, isolated_hermes_home):
        """Both stores are consulted; the newest finished receipt wins."""
        home = isolated_hermes_home
        _write_legacy_receipt(
            home,
            update_id="f" * 32,
            outcome="failed",
            finished_at="2026-10-19T12:07:37.711465+00:00",
            stamp="20261019_120737",
        )
        record = UpdateReceiptRecord(
            receipt_id="0" * 16,
            outcome="conflict",
            error="merge-conflict",
            rolled_back=False,
            acknowledged=False,
            started_at=1792490000.0,
            finished_at=1792500000.0,
        )
        write_pipeline_receipt(home, record)

        payload = read_latest_update_receipt_dict(home)

        assert payload is not None
        assert payload["receipt_id"] == "0" * 16
        assert payload["outcome"] == "conflict"

    def test_no_receipts_at_all_returns_none(self, isolated_hermes_home):
        assert read_latest_update_receipt_dict(isolated_hermes_home) is None


class TestLegacyPointerReceipt:
    """``latest.json`` is upstream's own "latest receipt" pointer, and the
    route has always served it. It is only a candidate when it is an UPDATE
    receipt: the pointer is overwritten by unrelated writers (plugin checks,
    fleet syncs), and the Desktop's pane reads any non-success outcome as
    "the last update failed"."""

    def _write_pointer(self, home: Path, payload: dict) -> None:
        directory = home / "logs" / "update_receipts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "latest.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_update_pointer_is_served(self, isolated_hermes_home):
        """Upstream's endpoint contract: the pointer IS the receipt."""
        home = isolated_hermes_home
        self._write_pointer(
            home,
            {
                "action_id": "d" * 32,
                "finished_at": "2026-10-20T12:07:37.711465+00:00",
                "outcome": "failed",
                "steps": [{"name": "merge", "ok": False, "detail": "conflict"}],
            },
        )

        payload = read_latest_update_receipt_dict(home)

        assert payload is not None
        assert payload["outcome"] == "failed"
        assert payload["steps"][0]["detail"] == "conflict"
        assert payload["acknowledged"] is False

    def test_plugin_check_pointer_is_not_served(self, isolated_hermes_home):
        """A plugin check in the pointer must never reach the pane: its
        outcome ("updates-available") reads as an update failure."""
        home = isolated_hermes_home
        self._write_pointer(
            home,
            {
                "finished_at": "2026-10-20T17:41:21.929478+00:00",
                "kind": "plugin-check",
                "outcome": "updates-available",
                "plugin_checks": [],
            },
        )

        assert read_latest_update_receipt_dict(home) is None

    def test_newer_archive_receipt_beats_the_pointer(self, isolated_hermes_home):
        home = isolated_hermes_home
        self._write_pointer(
            home,
            {
                "action_id": "a" * 32,
                "finished_at": "2026-10-19T12:07:37.711465+00:00",
                "outcome": "success",
            },
        )
        _write_legacy_receipt(
            home,
            update_id="b" * 32,
            outcome="failed",
            finished_at="2026-10-20T12:07:37.711465+00:00",
            stamp="20261020_120737",
        )

        payload = read_latest_update_receipt_dict(home)

        assert payload is not None
        assert payload["receipt_id"] == "b" * 32


class TestLegacyStoreAcknowledgement:
    """The acknowledgement is flipped ON THE RECEIPT, in whichever store
    holds it — acknowledging one run must never hide a different one."""

    def test_ack_flips_the_legacy_receipt(self, isolated_hermes_home):
        home = isolated_hermes_home
        update_id = "1" * 32
        path = _write_legacy_receipt(home, update_id=update_id)

        assert acknowledge_update_receipt(home, update_id) is True

        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["acknowledged"] is True
        # The reader now reports it as acknowledged, so the pane stays closed.
        payload = read_latest_update_receipt_dict(home)
        assert payload is not None
        assert payload["acknowledged"] is True

    def test_ack_is_idempotent(self, isolated_hermes_home):
        home = isolated_hermes_home
        update_id = "2" * 32
        _write_legacy_receipt(home, update_id=update_id)

        assert acknowledge_update_receipt(home, update_id) is True
        assert acknowledge_update_receipt(home, update_id) is True

    def test_ack_of_unknown_id_returns_false(self, isolated_hermes_home):
        home = isolated_hermes_home
        _write_legacy_receipt(home, update_id="3" * 32)

        assert acknowledge_update_receipt(home, "4" * 32) is False

    @pytest.mark.parametrize(
        "malicious_id",
        [
            "../escape",
            "..\\escape",
            "/absolute/path",
            "",
            "abc",
            "ZZZ1234567890abc",
            "abcd.json",
        ],
    )
    def test_ack_rejects_malicious_ids(self, isolated_hermes_home, malicious_id):
        """A receipt id that isn't a 32-hex update id must never reach the
        filesystem — the id is used to find the archive file, and a path
        separator in it could escape the receipts directory."""
        home = isolated_hermes_home
        _write_legacy_receipt(home, update_id="5" * 32)

        assert acknowledge_update_receipt(home, malicious_id) is False

    def test_ack_does_not_touch_other_receipts(self, isolated_hermes_home):
        """Acknowledging one run leaves every other receipt alone."""
        home = isolated_hermes_home
        first = _write_legacy_receipt(home, update_id="6" * 32)
        second = _write_legacy_receipt(home, update_id="7" * 32, stamp="20261020_120738")

        assert acknowledge_update_receipt(home, "6" * 32) is True

        assert json.loads(first.read_text(encoding="utf-8"))["acknowledged"] is True
        assert "acknowledged" not in json.loads(second.read_text(encoding="utf-8"))
