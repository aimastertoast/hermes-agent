"""Tests for the pipeline receipt contract (Task 3 of the update-permanent-fix plan).

The new pipeline types live alongside the legacy ``UpdateReceipt`` class in
``hermes_cli/update_receipt.py``. They are deliberately named
``UpdateReceiptRecord`` / ``write_pipeline_receipt`` /
``read_latest_pipeline_receipt`` / ``acknowledge_pipeline_receipt`` so the
dozens of existing imports of ``UpdateReceipt`` and ``read_latest_receipt()``
keep working. These tests exercise the new pipeline only.
"""
import json
import secrets

import pytest

from hermes_cli.update_receipt import (
    UpdateReceiptRecord,
    acknowledge_pipeline_receipt,
    read_latest_pipeline_receipt,
    write_pipeline_receipt,
)


def test_receipt_round_trip_preserves_new_fields(isolated_hermes_home):
    home = isolated_hermes_home
    receipt = UpdateReceiptRecord(
        receipt_id=secrets.token_hex(8),
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
    rid = secrets.token_hex(8)
    receipt = UpdateReceiptRecord(
        receipt_id=rid,
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
    acknowledge_pipeline_receipt(home, rid)
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
        receipt_id=secrets.token_hex(8),
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
    assert raw["receipt_id"] == receipt.receipt_id
    assert raw["ahead_disregarded"] == 7
    assert raw["pre_state"] == {"a": 1}
    assert raw["post_state"] == {"a": 2}
    # No leftover tmp files.
    tmp_files = list((home / "update_receipts").glob(".*.tmp"))
    assert tmp_files == []


def test_read_latest_pipeline_receipt_returns_none_when_pointer_missing_target(
    isolated_hermes_home,
):
    """latest.json points to a receipt file that doesn't exist."""
    home = isolated_hermes_home
    rdir = home / "update_receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "latest.json").write_text(json.dumps({"receipt_id": secrets.token_hex(8)}))
    assert read_latest_pipeline_receipt(home) is None


def test_read_latest_pipeline_receipt_returns_none_when_latest_json_corrupt(
    isolated_hermes_home,
):
    """Malformed latest.json — must NOT raise."""
    home = isolated_hermes_home
    rdir = home / "update_receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "latest.json").write_text("{this is not json")
    assert read_latest_pipeline_receipt(home) is None


def test_read_latest_pipeline_receipt_returns_none_when_receipt_file_corrupt(
    isolated_hermes_home,
):
    """Valid pointer but the receipt file itself is malformed."""
    home = isolated_hermes_home
    rdir = home / "update_receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    rid = secrets.token_hex(8)
    (rdir / "latest.json").write_text(json.dumps({"receipt_id": rid}))
    (rdir / f"{rid}.json").write_text("{also not json")
    assert read_latest_pipeline_receipt(home) is None


def test_acknowledge_pipeline_receipt_returns_false_when_missing(
    isolated_hermes_home,
):
    """acknowledge_pipeline_receipt on a non-existent receipt id is a no-op."""
    home = isolated_hermes_home
    rdir = home / "update_receipts"
    rdir.mkdir(parents=True, exist_ok=True)
    assert acknowledge_pipeline_receipt(home, secrets.token_hex(8)) is False
    # No file was created.
    assert list(rdir.iterdir()) == []


class TestPipelineReceiptPathTraversal:
    """Defense-in-depth: a ``receipt_id`` that isn't a ``secrets.token_hex(8)`` value
    must never reach the filesystem. The orchestrator always emits 16 lowercase
    hex chars, but the readers accept ids from ``latest.json`` (which any
    process with write access to the receipts dir can edit) and from the
    ack API (which a non-orchestrator caller can target with anything). Both
    surfaces must reject path-significant characters and unrelated filenames."""

    @pytest.mark.parametrize(
        "malicious_id",
        [
            "../escape",            # relative-path escape
            "..\\escape",           # Windows-style relative-path escape
            "../../etc/passwd",     # multi-hop escape
            "/absolute/path",       # absolute path
            "abc",                  # too short
            "ZZZ1234567890abc",     # wrong charset (uppercase)
            "g" * 17,               # too long
            "",                     # empty
            "abc/../def",           # embedded separator
            ".json.tmp",            # would clobber writer's atomic temp file
            "abcd.json",            # extension injection
            "ab cd ef gh ij kl",    # whitespace
        ],
    )
    def test_read_latest_rejects_malicious_receipt_id_in_pointer(
        self, isolated_hermes_home, monkeypatch, malicious_id
    ):
        """A ``latest.json`` whose ``receipt_id`` would resolve outside the
        receipts dir must read as ``None`` — never as the wrong file, never
        as a filesystem error."""
        home = isolated_hermes_home
        rdir = home / "update_receipts"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "latest.json").write_text(json.dumps({"receipt_id": malicious_id}))
        # Even if a file with the literal name exists somewhere under home,
        # the reader must not pick it up — proves the validation happens
        # before the filesystem access.
        outside_target = home / f"{malicious_id}.json"
        if not outside_target.exists():
            # We can't always create the literal name on every platform
            # (e.g. names containing separators), so only seed when safe.
            try:
                outside_target.write_text("{}")
            except OSError:
                pass
        assert read_latest_pipeline_receipt(home) is None

    @pytest.mark.parametrize(
        "malicious_id",
        [
            "../escape",
            "..\\escape",
            "../../etc/passwd",
            "/absolute/path",
            "abc",
            "ZZZ1234567890abc",
            "g" * 17,
            "",
            ".json.tmp",
            "abcd.json",
            "ab cd ef gh ij kl",
        ],
    )
    def test_acknowledge_rejects_malicious_receipt_id(
        self, isolated_hermes_home, monkeypatch, malicious_id
    ):
        """acknowledge_pipeline_receipt must never write outside the receipts
        dir, no matter what the caller passes. A malicious id must return
        ``False`` without producing a target file under the receipts dir,
        and without modifying any pre-existing file outside the dir."""
        home = isolated_hermes_home
        rdir = home / "update_receipts"
        rdir.mkdir(parents=True, exist_ok=True)
        # Seed a sentinel file outside the receipts dir; the ack must NOT
        # touch it.
        sentinel_name = "sentinel-target.json"
        sentinel_path = home / sentinel_name
        sentinel_path.write_text('{"acknowledged": false}')
        # The ack function must validate BEFORE any filesystem write.
        result = acknowledge_pipeline_receipt(home, malicious_id)
        assert result is False
        # No new file inside the receipts dir.
        assert list(rdir.iterdir()) == []
        # The sentinel outside the dir is untouched.
        assert sentinel_path.read_text() == '{"acknowledged": false}'

    def test_read_latest_accepts_valid_token_hex_id(self, isolated_hermes_home):
        """Sanity: a real ``secrets.token_hex(8)`` value is still accepted —
        the guard isn't over-broad."""
        home = isolated_hermes_home
        valid_id = secrets.token_hex(8)
        assert valid_id  # 16 lowercase hex chars
        rdir = home / "update_receipts"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "latest.json").write_text(json.dumps({"receipt_id": valid_id}))
        (rdir / f"{valid_id}.json").write_text(
            json.dumps({
                "receipt_id": valid_id,
                "outcome": "success",
                "error": None,
                "rolled_back": False,
                "acknowledged": False,
                "strategy": "merge",
                "applied_via": "user-click",
                "safe_classification": {"auto_apply_safe": False, "reasons": []},
                "steps": [],
                "ahead_disregarded": 0,
                "pre_state": {},
                "post_state": {},
            })
        )
        record = read_latest_pipeline_receipt(home)
        assert record is not None
        assert record.receipt_id == valid_id

    def test_acknowledge_accepts_valid_token_hex_id(self, isolated_hermes_home):
        """Sanity: a real ``secrets.token_hex(8)`` id is still accepted —
        the guard doesn't break the happy path."""
        from hermes_cli.update_receipt import UpdateReceiptRecord, write_pipeline_receipt

        home = isolated_hermes_home
        receipt = UpdateReceiptRecord(
            receipt_id=secrets.token_hex(8),
            outcome="success",
            error=None,
            rolled_back=False,
            acknowledged=False,
            strategy="merge",
            applied_via="user-click",
        )
        write_pipeline_receipt(home, receipt)
        assert acknowledge_pipeline_receipt(home, receipt.receipt_id) is True
