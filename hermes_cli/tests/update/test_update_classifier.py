from dataclasses import dataclass

import pytest
from hermes_cli.update_classifier import classify_safe


@dataclass
class FakeState:
    state_db_schema: int = 3
    config_yaml_bytes: bytes = b"version: 1\n"
    mcp_servers: dict | None = None
    local_commits_ahead: int = 0


def test_classify_safe_marks_schema_bump_unsafe():
    pre = FakeState(state_db_schema=3)
    post = FakeState(state_db_schema=4)
    assert classify_safe(pre, post, origin_moved=False, local_ahead=False) is False


def test_classify_safe_marks_config_change_unsafe():
    pre = FakeState(config_yaml_bytes=b"version: 1\n")
    post = FakeState(config_yaml_bytes=b"version: 1\napi_key: sk-fake-aaa\n")
    assert classify_safe(pre, post, origin_moved=False, local_ahead=False) is False


def test_classify_safe_marks_additive_mcp_change_safe():
    pre = FakeState(mcp_servers={"github": {"enabled": True}})
    post = FakeState(mcp_servers={"github": {"enabled": True}, "slack": {"enabled": True}})
    assert classify_safe(pre, post, origin_moved=False, local_ahead=False) is True


def test_classify_safe_marks_local_ahead_unsafe():
    pre = FakeState(local_commits_ahead=5)
    post = FakeState(local_commits_ahead=5)
    assert classify_safe(pre, post, origin_moved=False, local_ahead=True) is False


def test_classify_safe_marks_force_push_unsafe():
    pre = FakeState()
    post = FakeState()
    assert classify_safe(pre, post, origin_moved=True, local_ahead=False) is False


def test_classify_safe_is_pure():
    pre = FakeState(); post = FakeState()
    r1 = classify_safe(pre, post, origin_moved=False, local_ahead=False)
    r2 = classify_safe(pre, post, origin_moved=False, local_ahead=False)
    assert r1 == r2


@pytest.mark.parametrize("schema", [1, 2, 3, 5])
def test_classify_safe_marks_any_schema_change_unsafe(schema):
    pre = FakeState(state_db_schema=schema)
    post = FakeState(state_db_schema=schema + 1)
    assert classify_safe(pre, post, origin_moved=False, local_ahead=False) is False


def test_classify_safe_marks_removed_mcp_server_unsafe():
    """A pre-state server that no longer exists post-update = unsafe."""
    pre = FakeState(mcp_servers={"github": {"enabled": True}, "slack": {"enabled": True}})
    post = FakeState(mcp_servers={"github": {"enabled": True}})
    assert classify_safe(pre, post, origin_moved=False, local_ahead=False) is False


def test_classify_safe_marks_changed_mcp_server_unsafe():
    """A pre-state server whose config changed post-update = unsafe."""
    pre = FakeState(mcp_servers={"github": {"enabled": True, "token": "x"}})
    post = FakeState(mcp_servers={"github": {"enabled": True, "token": "y"}})
    assert classify_safe(pre, post, origin_moved=False, local_ahead=False) is False