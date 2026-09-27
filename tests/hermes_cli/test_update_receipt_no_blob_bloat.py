"""Regression: receipts must NOT embed raw state.db / config.yaml content.

Background
----------
``UpdateSnapshot`` carries ``state_db_bytes`` and ``config_yaml_bytes`` because
``restore_from_snapshot`` writes them back to disk on rollback. Those fields
must NOT appear in the on-disk receipt — the receipt should hold hashes +
metadata only. Otherwise a typical state.db (~626 MB) produces a ~3.4 GB
receipt (the content is embedded in both ``pre_state`` and ``post_state``).

This test pins the serialization contract so a future refactor cannot
re-introduce the bloat.
"""

import json

import pytest

from hermes_cli import update_orchestrator as uo
from hermes_cli.update_snapshot import UpdateSnapshot


def _snapshot_with_blobs() -> UpdateSnapshot:
    return UpdateSnapshot(
        state_db_hash="a" * 64,
        config_yaml_hash="b" * 64,
        mcp_servers={"hermes": {"command": "echo"}},
        gateway_pid=1234,
        state_db_schema=7,
        schema_version_field="schema_version",
        # 1 MB blob in each — large enough to make any leak obvious.
        config_yaml_bytes=b"x" * (1024 * 1024),
        state_db_bytes=b"y" * (1024 * 1024),
        local_commits_ahead=2,
    )


def test_state_to_dict_strips_raw_byte_fields():
    """The byte blobs must be dropped at the serialization boundary."""
    d = uo._state_to_dict(_snapshot_with_blobs())

    assert "state_db_bytes" not in d, "raw state.db blob leaked into receipt dict"
    assert "config_yaml_bytes" not in d, "raw config.yaml blob leaked into receipt dict"

    # Hashes + metadata must still be present.
    assert d["state_db_hash"] == "a" * 64
    assert d["config_yaml_hash"] == "b" * 64
    assert d["state_db_schema"] == 7
    assert d["gateway_pid"] == 1234
    assert d["local_commits_ahead"] == 2


def test_state_to_dict_on_duck_typed_state_strips_raw_byte_fields():
    """The fallback ``__dict__`` path used by failure-mode tests must also strip bytes."""
    raw = _snapshot_with_blobs()
    # Simulate the bare ``type("S", (), {...})()`` shape used in monkeypatched
    # tests so the fallback branch is exercised.
    duck = type("S", (), {})()
    for field_name in (
        "state_db_hash", "config_yaml_hash", "mcp_servers", "gateway_pid",
        "state_db_schema", "schema_version_field",
        "config_yaml_bytes", "state_db_bytes", "local_commits_ahead",
    ):
        setattr(duck, field_name, getattr(raw, field_name))

    d = uo._state_to_dict(duck)

    assert "state_db_bytes" not in d
    assert "config_yaml_bytes" not in d
    assert d["state_db_hash"] == "a" * 64
    assert d["local_commits_ahead"] == 2


def test_receipt_json_size_does_not_balloon_when_state_db_is_large():
    """End-to-end: feeding a snapshot with large blobs to ``json.dumps``
    must produce a small payload. If this test starts failing, the blob
    is back in the receipt path."""
    record = {
        "receipt_id": "test123",
        "pre_state": uo._state_to_dict(_snapshot_with_blobs()),
        "post_state": uo._state_to_dict(_snapshot_with_blobs()),
        "steps": [],
    }
    payload = json.dumps(record, default=str)

    # 1 MB × 2 blobs × 2 states = ~4 MB if not stripped; << 100 KB if stripped.
    assert len(payload) < 100_000, (
        f"Receipt payload is {len(payload):,} bytes — raw blobs leaked back into "
        f"the receipt. Expected < 100 KB once stripped."
    )