"""Capture and restore pre-update state. See spec Section 2.2."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class UpdateSnapshot:
    state_db_hash: str
    config_yaml_hash: str
    mcp_servers: dict = field(default_factory=dict)
    gateway_pid: Optional[int] = None
    state_db_schema: int = 0
    schema_version_field: str = "schema_version"
    config_yaml_bytes: bytes = b""
    state_db_bytes: bytes = b""
    # How many local commits are ahead of origin/<channel> when this snapshot
    # was captured. Used by the orchestrator (Task 6) via classify_safe's
    # _StateLike protocol. Default 0 so callers that don't care (e.g. the
    # round-trip snapshot tests) don't need to pass it.
    local_commits_ahead: int = 0


def capture_pre_state(home: Path, repo: Path | None) -> UpdateSnapshot:
    """Snapshot state.db, config.yaml, MCP server config, and gateway PID."""
    state_db_path = home / "state.db"
    config_yaml_path = home / "config.yaml"

    state_db_bytes = state_db_path.read_bytes() if state_db_path.exists() else b""
    config_yaml_bytes = config_yaml_path.read_bytes() if config_yaml_path.exists() else b""

    state_db_hash = hashlib.sha256(state_db_bytes).hexdigest()
    config_yaml_hash = hashlib.sha256(config_yaml_bytes).hexdigest()

    state_db_schema = _read_schema_version(state_db_path)

    return UpdateSnapshot(
        state_db_hash=state_db_hash,
        config_yaml_hash=config_yaml_hash,
        mcp_servers={},  # populated by orchestrator from MCP registry
        gateway_pid=None,  # populated by orchestrator from gateway process
        state_db_schema=state_db_schema,
        schema_version_field="schema_version",
        config_yaml_bytes=config_yaml_bytes,
        state_db_bytes=state_db_bytes,
    )


def restore_from_snapshot(home: Path, repo: Path | None, snapshot: UpdateSnapshot) -> bool:
    """Restore state.db and config.yaml from snapshot. Returns True iff verified."""
    state_db_path = home / "state.db"
    config_yaml_path = home / "config.yaml"

    state_db_path.write_bytes(snapshot.state_db_bytes)
    config_yaml_path.write_bytes(snapshot.config_yaml_bytes)

    # Verify post-restore hashes
    actual_db_hash = hashlib.sha256(state_db_path.read_bytes()).hexdigest()
    actual_yaml_hash = hashlib.sha256(config_yaml_path.read_bytes()).hexdigest()

    return (
        actual_db_hash == snapshot.state_db_hash
        and actual_yaml_hash == snapshot.config_yaml_hash
    )


def _read_schema_version(state_db_path: Path) -> int:
    """Read schema_version from state.db. Returns 0 if missing/empty."""
    if not state_db_path.exists() or state_db_path.stat().st_size == 0:
        return 0
    try:
        conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return 0
