import hashlib

import pytest
from hermes_cli.update_snapshot import (
    UpdateSnapshot,
    capture_pre_state,
    restore_from_snapshot,
)


def test_capture_pre_state_records_hashes(isolated_hermes_home, fake_gateway):
    home = isolated_hermes_home
    snap = capture_pre_state(home, repo=None)

    expected_db_hash = hashlib.sha256(b"").hexdigest()
    expected_yaml_hash = hashlib.sha256(b"version: 1\nprofile: default\nmcp_servers: {}\n").hexdigest()
    assert snap.state_db_hash == expected_db_hash
    assert snap.config_yaml_hash == expected_yaml_hash


def test_restore_from_snapshot_round_trips(isolated_hermes_home, fake_gateway, tmp_path):
    home = isolated_hermes_home
    # Capture snapshot of the initial (empty) state.db BEFORE modifying it,
    # so the snapshot retains the empty bytes for restoration.
    snap = capture_pre_state(home, repo=None)
    # Modify state.db to something different
    (home / "state.db").write_bytes(b"modified-during-update")
    # Restore: snapshot still has the empty bytes
    assert restore_from_snapshot(home, repo=None, snapshot=snap) is True
    assert (home / "state.db").read_bytes() == b""


def test_restore_verifies_post_hash_matches(tmp_path):
    home = tmp_path / "fake-home"
    home.mkdir()
    (home / "state.db").write_bytes(b"original")
    snap = UpdateSnapshot(
        state_db_hash=hashlib.sha256(b"original").hexdigest(),
        config_yaml_hash=hashlib.sha256(b"").hexdigest(),
        mcp_servers={}, gateway_pid=None, state_db_schema=3,
        schema_version_field="schema_version",
        config_yaml_bytes=b"", state_db_bytes=b"original",
    )
    assert restore_from_snapshot(home, repo=None, snapshot=snap) is True
