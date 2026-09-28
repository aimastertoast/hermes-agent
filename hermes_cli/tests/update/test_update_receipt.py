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
    latest_pipeline_receipt_summary,
    read_latest_pipeline_receipt,
    read_latest_pipeline_receipt_dict,
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


# ─────────────────────────────────────────────────────────────────────────────
# M1: profile-aware JSON serialization for the API route
#
# The desktop overlay calls ``/api/hermes/update/receipt`` with
# ``?profile=X`` (``profileScoped()``). The Python route honors the profile
# by flipping ``HERMES_HOME`` via ``_config_profile_scope`` and then calls
# ``read_latest_pipeline_receipt_dict(home)`` to get a JSON-ready dict.
#
# What the tests pin:
# 1. ``read_latest_pipeline_receipt_dict`` returns None when no receipt exists.
# 2. It returns a plain dict (not a dataclass) on success — the FastAPI
#    response layer can't serialize dataclasses directly.
# 3. ``latest_pipeline_receipt_summary`` returns None when no receipt exists
#    AND a dict with the ``UpdateReceiptSummary`` shape when one does.
# 4. Profile scoping: a receipt written to profile A's home is INVISIBLE
#    when read from profile B's home. Mirrors the desktop's
#    ``?profile=X`` semantics.
# ─────────────────────────────────────────────────────────────────────────────

class TestPipelineReceiptJsonSerialization:
    """The FastAPI route handler can't serialize dataclasses; these helpers
    bridge the pipeline reader to a JSON-ready dict."""

    def test_read_dict_returns_none_when_no_receipt(self, isolated_hermes_home):
        """No pipeline receipt on disk → None. Caller raises 404."""
        assert read_latest_pipeline_receipt_dict(isolated_hermes_home) is None

    def test_read_dict_returns_plain_dict_on_success(self, isolated_hermes_home):
        """A receipt on disk serializes to a plain dict (NOT a dataclass) so
        FastAPI's JSON encoder accepts it without an explicit ``model_dump``."""
        receipt_id = secrets.token_hex(8)
        write_pipeline_receipt(
            isolated_hermes_home,
            UpdateReceiptRecord(
                receipt_id=receipt_id,
                outcome="success",
                error=None,
                rolled_back=False,
                acknowledged=False,
                strategy="merge",
                applied_via="user-click",
            ),
        )
        payload = read_latest_pipeline_receipt_dict(isolated_hermes_home)
        assert isinstance(payload, dict)
        assert payload["receipt_id"] == receipt_id
        assert payload["outcome"] == "success"

    def test_summary_returns_none_when_no_receipt(self, isolated_hermes_home):
        assert latest_pipeline_receipt_summary(isolated_hermes_home) is None

    def test_summary_shape_matches_desktop_contract(self, isolated_hermes_home):
        """The summary keys mirror ``UpdateReceiptSummary`` in the desktop
        type definitions. Pinning the shape here so a future refactor that
        drops a key (e.g. ``fleet_states``) breaks the test."""
        write_pipeline_receipt(
            isolated_hermes_home,
            UpdateReceiptRecord(
                receipt_id=secrets.token_hex(8),
                outcome="success",
                error=None,
                rolled_back=False,
                acknowledged=False,
            ),
        )
        summary = latest_pipeline_receipt_summary(isolated_hermes_home)
        assert summary is not None
        assert set(summary.keys()) == {
            "outcome",
            "started_at",
            "finished_at",
            "pre_sha",
            "post_sha",
            "post_version",
            "fleet_states",
        }
        assert summary["outcome"] == "success"
        # Orchestrator doesn't track these — null is correct.
        assert summary["started_at"] is None
        assert summary["finished_at"] is None
        assert summary["pre_sha"] is None
        assert summary["post_sha"] is None
        assert summary["post_version"] is None
        # Single-host orchestrator; no fleet.
        assert summary["fleet_states"] == []


class TestPipelineReceiptProfileScoping:
    """M1: profile-scoped reads — each profile sees only its own receipts.

    The desktop's ``profileScoped()`` adds ``?profile=X`` to the receipt
    API call; the route flips ``HERMES_HOME`` to the target profile's home
    via ``_config_profile_scope``. These tests bypass the FastAPI layer
    and exercise the read helpers directly so the contract is pinned
    without the Starlette dependency.
    """

    def test_receipt_isolated_between_profiles(self, tmp_path, monkeypatch):
        """A receipt written to profile A's home is INVISIBLE when read from
        profile B's home. Mirrors the desktop's ``?profile=X`` semantics."""
        from hermes_constants import set_hermes_home_override

        home_a = tmp_path / "profile-a"
        home_b = tmp_path / "profile-b"
        home_a.mkdir()
        home_b.mkdir()

        # Write a receipt to profile A.
        rid = secrets.token_hex(8)
        write_pipeline_receipt(
            home_a,
            UpdateReceiptRecord(
                receipt_id=rid,
                outcome="success",
                error=None,
                rolled_back=False,
                acknowledged=False,
            ),
        )

        # Read from profile A → visible.
        token_a = set_hermes_home_override(home_a)
        try:
            payload = read_latest_pipeline_receipt_dict(home_a)
            assert payload is not None
            assert payload["receipt_id"] == rid
        finally:
            from hermes_constants import reset_hermes_home_override
            reset_hermes_home_override(token_a)

        # Read from profile B → invisible (the request landed on A's home,
        # but B's home has no receipt).
        assert read_latest_pipeline_receipt_dict(home_b) is None

    def test_summary_inherits_profile_isolation(self, tmp_path):
        """Summary reads follow the same per-home scoping rule."""
        from hermes_cli.update_receipt import latest_pipeline_receipt_summary

        home_a = tmp_path / "profile-a"
        home_b = tmp_path / "profile-b"
        home_a.mkdir()
        home_b.mkdir()

        write_pipeline_receipt(
            home_a,
            UpdateReceiptRecord(
                receipt_id=secrets.token_hex(8),
                outcome="conflict",
                error="merge-conflict",
                rolled_back=False,
                acknowledged=False,
            ),
        )
        # Profile A sees the summary; profile B does not.
        assert latest_pipeline_receipt_summary(home_a) is not None
        assert latest_pipeline_receipt_summary(home_b) is None
