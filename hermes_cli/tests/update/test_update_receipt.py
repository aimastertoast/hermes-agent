"""Tests for the pipeline receipt contract (Task 3 of the update-permanent-fix plan).

The new pipeline types live alongside the legacy ``UpdateReceipt`` class in
``hermes_cli/update_receipt.py``. They are deliberately named
``UpdateReceiptRecord`` / ``write_pipeline_receipt`` /
``read_latest_pipeline_receipt`` / ``acknowledge_pipeline_receipt`` so the
dozens of existing imports of ``UpdateReceipt`` and ``read_latest_receipt()``
keep working. These tests exercise the new pipeline only.
"""
import json

from hermes_cli.update_receipt import (
    UpdateReceiptRecord,
    acknowledge_pipeline_receipt,
    read_latest_pipeline_receipt,
    write_pipeline_receipt,
)


def test_receipt_round_trip_preserves_new_fields(isolated_hermes_home):
    home = isolated_hermes_home
    receipt = UpdateReceiptRecord(
        receipt_id="abc123",
        outcome="success",
        error=None,
        rolled_back=False,
        acknowledged=False,
        strategy="merge",
        applied_via="user-click",
        safe_classification={"auto_apply_safe": True, "reasons": []},
        steps=[{"name": "preflight", "ok": True, "detail": None}],
        ahead_disregarded=0,
        pre_state={"state_db_hash": "x"},
        post_state={"state_db_hash": "x"},
    )
    write_pipeline_receipt(home, receipt)
    loaded = read_latest_pipeline_receipt(home)
    assert loaded is not None
    assert loaded.outcome == "success"
    assert loaded.acknowledged is False
    assert loaded.applied_via == "user-click"
    assert len(loaded.steps) == 1
    assert loaded.safe_classification == {"auto_apply_safe": True, "reasons": []}


def test_acknowledge_pipeline_receipt_flips_flag(isolated_hermes_home):
    home = isolated_hermes_home
    receipt = UpdateReceiptRecord(
        receipt_id="abc",
        outcome="failed",
        error="x",
        rolled_back=True,
        acknowledged=False,
        strategy="merge",
        applied_via="user-click",
        safe_classification={"auto_apply_safe": False, "reasons": ["x"]},
        steps=[],
        ahead_disregarded=0,
        pre_state={},
        post_state={},
    )
    write_pipeline_receipt(home, receipt)
    acknowledge_pipeline_receipt(home, "abc")
    loaded = read_latest_pipeline_receipt(home)
    assert loaded.acknowledged is True


def test_read_latest_pipeline_receipt_returns_none_when_no_receipts(
    isolated_hermes_home,
):
    assert read_latest_pipeline_receipt(isolated_hermes_home) is None


def test_write_pipeline_receipt_is_atomic(isolated_hermes_home):
    """Atomicity contract: the file must exist with full payload (not a torn write)."""
    home = isolated_hermes_home
    receipt = UpdateReceiptRecord(
        receipt_id="atomic-id",
        outcome="success",
        error=None,
        rolled_back=False,
        acknowledged=False,
        strategy="merge",
        applied_via="user-click",
        safe_classification={"auto_apply_safe": True, "reasons": []},
        steps=[{"name": "x", "ok": True, "detail": "y"}],
        ahead_disregarded=7,
        pre_state={"a": 1},
        post_state={"a": 2},
    )
    target = write_pipeline_receipt(home, receipt)
    # File exists, is valid JSON, has every field.
    assert target.exists()
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["receipt_id"] == "atomic-id"
    assert raw["ahead_disregarded"] == 7
    assert raw["pre_state"] == {"a": 1}
    assert raw["post_state"] == {"a": 2}
    # No leftover tmp files.
    tmp_files = list((home / "update_receipts").glob(".*.tmp"))
    assert tmp_files == []
