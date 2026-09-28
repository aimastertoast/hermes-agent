"""Pure function: is the post-update state safe to auto-apply?

No I/O, no time, no globals — see spec Section 2.1.
"""
from __future__ import annotations

from typing import Protocol


class _StateLike(Protocol):
    state_db_schema: int
    config_yaml_bytes: bytes
    mcp_servers: dict | None
    local_commits_ahead: int


def classify_safe(
    pre: _StateLike,
    post: _StateLike,
    *,
    origin_moved: bool,
    local_ahead: bool,
) -> bool:
    """Decides whether a post-update state can be auto-applied.

    Returns True ONLY if all of:
    - state.db schema version unchanged
    - config.yaml hash unchanged
    - MCP servers are an additive superset (or pre-state had none)
    - local is NOT ahead of origin
    - origin did not force-push mid-fetch
    """
    if pre.state_db_schema != post.state_db_schema:
        return False
    if pre.config_yaml_bytes != post.config_yaml_bytes:
        return False
    if not _mcp_is_additive_superset(pre.mcp_servers, post.mcp_servers):
        return False
    if local_ahead:
        return False
    if origin_moved:
        return False
    return True


def _mcp_is_additive_superset(pre: dict | None, post: dict | None) -> bool:
    """Post must be a superset of pre: every pre server still present with same config.

    New servers may be added (they can be opt-in). Removed/changed servers = unsafe.
    """
    pre = pre or {}
    post = post or {}
    for name, cfg in pre.items():
        if name not in post:
            return False
        if post[name] != cfg:
            return False
    return True
