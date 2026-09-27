"""G1 guard rail: config writes must never silently lose user-data keys.

Evidence: 2026-09-24, Nana's whole ``mcp_servers`` map was deleted by a
full-replace save (``mcp_servers`` is not in DEFAULT_CONFIG and leaf-path
preservation could not protect the node). Spec:
docs/superpowers/specs/2026-09-25-hermes-guard-rails-design.md §3.
"""
import copy
import logging

import pytest
import yaml

from hermes_cli.config import (
    DEFAULT_CONFIG,
    ConfigWriteGuardError,
    _explicit_config_paths,
    atomic_config_write,
    load_config,
    read_raw_config,
    save_config,
)


class TestExplicitConfigPathsDictNodes:
    def test_unknown_top_level_dict_records_node_and_leaves(self):
        raw = {"mcp_servers": {"github": {"command": "npx", "args": ["-y", "pkg"]}}}
        paths = _explicit_config_paths(raw)
        assert ("mcp_servers",) in paths
        assert ("mcp_servers", "github") in paths
        assert ("mcp_servers", "github", "command") in paths

    def test_known_defaulted_dict_records_leaves_only(self):
        # ``gateway`` exists in DEFAULT_CONFIG as a dict: the node must NOT be
        # preserved (that would keep caller-injected default-equal leaves too),
        # but unknown sub-dicts under it gain a node.
        assert isinstance(DEFAULT_CONFIG.get("gateway"), dict)
        raw = {"gateway": {"standalone": True, "extra": {"a": 1}}}
        paths = _explicit_config_paths(raw)
        assert ("gateway",) not in paths
        assert ("gateway", "standalone") in paths
        assert ("gateway", "extra") in paths

    def test_scalar_known_key_still_records_leaf(self):
        raw = {"proxy": {"label": "nana"}}
        paths = _explicit_config_paths(raw)
        assert ("proxy", "label") in paths


SEED_CONFIG = {
    "mcp_servers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "enabled": True},
        "chrome-devtools": {"command": "npx", "args": ["-y", "chrome-devtools-mcp"], "enabled": True},
    },
    "custom_providers": {"ark": {"base_url": "https://ark.example"}},
}


@pytest.fixture
def seeded_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME whose config.yaml carries user-data keys."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(SEED_CONFIG), encoding="utf-8")
    return tmp_path


class TestSaveConfigPreservesUserData:
    def test_omitted_mcp_servers_is_represerved_with_warning(self, seeded_home, caplog):
        incoming = {"custom_providers": {"ark": {"base_url": "https://ark.example"}}}
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            save_config(incoming)
        raw = read_raw_config()
        assert set(raw["mcp_servers"]) == set(SEED_CONFIG["mcp_servers"])
        assert any("mcp_servers" in record.message for record in caplog.records)

    def test_removed_keys_makes_removal_explicit_and_silent(self, seeded_home, caplog):
        incoming = {"custom_providers": {"ark": {"base_url": "https://ark.example"}}}
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            save_config(incoming, removed_keys={"mcp_servers"})
        raw = read_raw_config()
        assert "mcp_servers" not in raw
        assert not [r for r in caplog.records if "mcp_servers" in r.message]

    def test_default_config_key_omission_still_strips(self, seeded_home):
        # Spec §3.2: keys IN DEFAULT_CONFIG keep today's strip-defaults behavior.
        assert "model" in DEFAULT_CONFIG
        save_config({**SEED_CONFIG, "model": {"default": "gpt-x", "provider": "openai"}})
        save_config({"mcp_servers": SEED_CONFIG["mcp_servers"],
                     "custom_providers": SEED_CONFIG["custom_providers"]})
        raw = read_raw_config()
        assert "model" not in raw

    def test_caller_dict_is_not_mutated(self, seeded_home):
        incoming = {"custom_providers": {"ark": {"base_url": "https://ark.example"}}}
        snapshot = {k: dict(v) for k, v in incoming.items()}
        save_config(incoming)
        assert incoming == snapshot


class TestLastServerRemoval:
    """G1 guard must not resurrect mcp_servers when the last server is removed."""

    def test_remove_last_server_pops_section_from_disk(self, seeded_home):
        from hermes_cli.mcp_config import _remove_mcp_server

        assert _remove_mcp_server("github") is True
        assert _remove_mcp_server("chrome-devtools") is True
        raw = read_raw_config()
        assert "mcp_servers" not in raw
        # Other user-data keys survive.
        assert "custom_providers" in raw

    def test_remove_second_server_keeps_section(self, seeded_home):
        from hermes_cli.mcp_config import _remove_mcp_server

        assert _remove_mcp_server("github") is True
        raw = read_raw_config()
        # chrome-devtools is still there.
        assert "mcp_servers" in raw
        assert set(raw["mcp_servers"]) == {"chrome-devtools"}


class TestMigrationGuardIsHard:
    """_persist_migration must stay hard: an undeclared user-data drop triggers the guard."""

    def test_undeclared_drop_on_migration_path_is_represerved(self, seeded_home, caplog):
        from hermes_cli.config import _persist_migration

        # Simulate a migration step that accidentally omits mcp_servers (the on-disk raw dict
        # has it, but the migrated config doesn't, and removed_keys is not declared).
        migrated = {"custom_providers": SEED_CONFIG["custom_providers"]}
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            _persist_migration(migrated)
        raw = read_raw_config()
        assert "mcp_servers" in raw
        assert any("mcp_servers" in record.message for record in caplog.records)

    def test_declared_drop_on_migration_path_is_silent(self, seeded_home, caplog):
        from hermes_cli.config import _persist_migration

        migrated = {"custom_providers": SEED_CONFIG["custom_providers"]}
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            _persist_migration(migrated, removed_keys={"mcp_servers"})
        raw = read_raw_config()
        assert "mcp_servers" not in raw
        assert not [r for r in caplog.records if "mcp_servers" in r.message]


class TestAtomicConfigWriteIsGuarded:
    """The G1 guard must sit at the chokepoint, not only in ``save_config``.

    Regression: the guard was ``save_config``-local, so the ~17 production writers
    that call ``atomic_config_write`` directly (``hermes login``, ``doctor_config``,
    ``credential_lifecycle``, the Telegram adapter, gateway slash commands, …)
    still dropped an omitted user-data key — the exact 2026-09-24 ``mcp_servers``
    loss, one layer down.
    """

    INCOMING = {"custom_providers": {"ark": {"base_url": "https://ark.example"}}}

    def _raw(self, home):
        return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))

    def test_direct_write_omitting_mcp_servers_is_represerved(self, seeded_home, caplog):
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            atomic_config_write(seeded_home / "config.yaml", self.INCOMING)

        raw = self._raw(seeded_home)
        assert set(raw["mcp_servers"]) == set(SEED_CONFIG["mcp_servers"])
        assert any("mcp_servers" in record.message for record in caplog.records)

    def test_direct_write_honours_removed_keys(self, seeded_home, caplog):
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            atomic_config_write(seeded_home / "config.yaml", self.INCOMING,
                                removed_keys={"mcp_servers"})

        assert "mcp_servers" not in self._raw(seeded_home)
        assert not [r for r in caplog.records if "mcp_servers" in r.message]

    def test_direct_write_to_a_fresh_path_writes_exactly_the_data(self, tmp_path):
        """Nothing on disk → nothing to protect, and no guard noise."""
        target = tmp_path / "config.yaml"
        atomic_config_write(target, self.INCOMING)
        assert set(self._raw(tmp_path)) == set(self.INCOMING)

    def test_direct_write_does_not_mutate_the_caller_dict(self, seeded_home):
        incoming = copy.deepcopy(self.INCOMING)
        atomic_config_write(seeded_home / "config.yaml", incoming)
        assert incoming == self.INCOMING

    def test_post_write_invariant_covers_direct_writers(self, seeded_home, monkeypatch):
        """A drop below the chokepoint must raise and restore pre-write bytes."""
        import utils

        real_save = utils.atomic_roundtrip_yaml_save

        def tampering_save(path, data, **kwargs):
            real_save(path, {k: v for k, v in data.items() if k != "mcp_servers"}, **kwargs)

        monkeypatch.setattr(utils, "atomic_roundtrip_yaml_save", tampering_save)
        before = (seeded_home / "config.yaml").read_bytes()

        with pytest.raises(ConfigWriteGuardError) as excinfo:
            atomic_config_write(seeded_home / "config.yaml", self.INCOMING)

        assert "mcp_servers" in str(excinfo.value)
        assert (seeded_home / "config.yaml").read_bytes() == before


class TestUnsetStillRemoves:
    """``hermes config unset`` is a deliberate drop — the guard must not undo it.

    Regression risk introduced by moving the G1 guard into ``atomic_config_write``:
    an unset that pops a whole root section looks identical to a partial writer that
    forgot the section, so the guard would re-preserve it and the unset would silently
    no-op. The explicit ``removed_keys`` pass-through is what keeps ``config unset``
    working.
    """

    def test_unset_of_a_nested_key_keeps_the_rest_of_the_section(self, seeded_home, capsys):
        from hermes_cli.config import unset_config_value

        unset_config_value("mcp_servers.github")
        capsys.readouterr()

        raw = read_raw_config()
        assert set(raw["mcp_servers"]) == {"chrome-devtools"}
        assert "custom_providers" in raw

    def test_unset_of_a_whole_root_section_removes_it(self, seeded_home, capsys):
        from hermes_cli.config import unset_config_value

        unset_config_value("mcp_servers")
        capsys.readouterr()

        raw = read_raw_config()
        assert "mcp_servers" not in raw
        assert "custom_providers" in raw


class TestPostWriteInvariant:
    def test_tampered_write_raises_and_restores_pre_write_bytes(self, seeded_home, monkeypatch):
        # The guard now lives at the ``atomic_config_write`` chokepoint, so the
        # injection point for "any layer below drops the key" moved one layer
        # down to ``utils.atomic_roundtrip_yaml_save``.
        import utils

        real_save = utils.atomic_roundtrip_yaml_save

        def tampering_save(path, data, **kwargs):
            # Simulate ANY layer below the chokepoint dropping the key (the exact
            # class of loss the guard exists to catch).
            real_save(path, {k: v for k, v in data.items() if k != "mcp_servers"}, **kwargs)

        monkeypatch.setattr(utils, "atomic_roundtrip_yaml_save", tampering_save)
        before = (seeded_home / "config.yaml").read_bytes()

        with pytest.raises(ConfigWriteGuardError) as excinfo:
            save_config({"custom_providers": {"ark": {"base_url": "https://ark.example"}}})

        assert "mcp_servers" in str(excinfo.value)
        assert (seeded_home / "config.yaml").read_bytes() == before

    def test_untampered_write_does_not_raise(self, seeded_home):
        save_config({**SEED_CONFIG, "custom_providers": {"ark": {"base_url": "https://ark.example"}}})

    def test_removed_key_does_not_trip_the_invariant(self, seeded_home):
        save_config({"custom_providers": {"ark": {"base_url": "https://ark.example"}}},
                    removed_keys={"mcp_servers"})
        assert "mcp_servers" not in read_raw_config()
