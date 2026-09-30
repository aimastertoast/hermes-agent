"""The mcp.json editor owns the whole ``mcp_servers`` map.

An empty map must clear the section, a populated one must replace it
wholesale, and one bad entry must reject the entire save. A silent partial
clear here is exactly the 2026-09-24 loss this path exists to prevent."""
import yaml

from hermes_cli.mcp_config import _replace_mcp_servers
from hermes_cli.config import read_raw_config


def _seed_home(tmp_path, monkeypatch, servers):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"mcp_servers": servers}), encoding="utf-8")


def test_empty_map_removes_mcp_servers(tmp_path, monkeypatch):
    _seed_home(tmp_path, monkeypatch, {"github": {"command": "npx", "enabled": True}})
    ok, issues = _replace_mcp_servers({})
    assert ok and issues == []
    assert "mcp_servers" not in read_raw_config()


def test_non_empty_map_replaces_wholesale(tmp_path, monkeypatch):
    _seed_home(tmp_path, monkeypatch, {
        "github": {"command": "npx", "enabled": True},
        "old": {"command": "npx", "enabled": False},
    })
    ok, issues = _replace_mcp_servers({"fresh": {"url": "https://mcp.example", "enabled": True}})
    assert ok and issues == []
    raw = read_raw_config()
    assert set(raw["mcp_servers"]) == {"fresh"}


def test_invalid_entry_rejects_whole_save(tmp_path, monkeypatch):
    _seed_home(tmp_path, monkeypatch, {"github": {"command": "npx", "enabled": True}})
    ok, issues = _replace_mcp_servers({"bad": "not-a-dict"})
    assert not ok and issues
    assert set(read_raw_config()["mcp_servers"]) == {"github"}
