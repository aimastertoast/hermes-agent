"""G3 guard rail: `hermes update` verifies (config parity, gateway liveness,
MCP fingerprint parity) against a pre-update snapshot before reporting
success; violations roll back. Spec §5."""
import json
import socket

import pytest
import yaml

from hermes_cli import update_verification as uv


@pytest.fixture
def two_homes(tmp_path, monkeypatch):
    root = tmp_path / "root"
    bobby = root / "profiles" / "bobby"
    bobby.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    seeds = (
        (root, {"github": {"command": "npx", "args": ["-y", "pkg"], "enabled": True}}),
        (bobby, {"lovable": {"url": "https://mcp.example", "enabled": False}}),
    )
    for home, servers in seeds:
        (home / "config.yaml").write_text(yaml.safe_dump({
            "mcp_servers": servers,
            "custom_providers": {"ark": {"base_url": "https://ark.example"}},
        }), encoding="utf-8")
    return {"root": root, "bobby": bobby}


def test_capture_roundtrip_and_parity(two_homes):
    state = uv.capture_pre_state(
        [("default", two_homes["root"]), ("bobby", two_homes["bobby"])], backup=False)
    assert state["profiles"]["bobby"]["root_keys"] == ["custom_providers", "mcp_servers"]
    assert state["profiles"]["bobby"]["mcp_fingerprint"] == {
        "lovable": {"enabled": False, "command": None, "url": "https://mcp.example"}}
    assert state["gateway_was_running"] is False  # no gateway_state.json in tmp home
    ok, _ = uv.check_config_parity(state)
    assert ok
    ok, _ = uv.check_mcp_fingerprint_parity(state)
    assert ok


def test_dropped_key_fails_config_parity(two_homes):
    state = uv.capture_pre_state([("default", two_homes["root"])], backup=False)
    (two_homes["root"] / "config.yaml").write_text(
        yaml.safe_dump({"custom_providers": {"ark": {"base_url": "https://ark.example"}}}),
        encoding="utf-8")
    ok, detail = uv.check_config_parity(state)
    assert not ok
    assert "mcp_servers" in detail


def test_fingerprint_drift_detected(two_homes):
    state = uv.capture_pre_state([("bobby", two_homes["bobby"])], backup=False)
    raw = yaml.safe_load((two_homes["bobby"] / "config.yaml").read_text(encoding="utf-8"))
    raw["mcp_servers"]["lovable"]["enabled"] = True
    (two_homes["bobby"] / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    ok, detail = uv.check_mcp_fingerprint_parity(state)
    assert not ok
    assert "changed" in detail


def test_fingerprint_never_contains_env_or_args_secrets(two_homes):
    state = uv.capture_pre_state([("default", two_homes["root"])], backup=False)
    blob = json.dumps(state)
    assert "args" not in state["profiles"]["default"]["mcp_fingerprint"]["github"]
    assert "env" not in blob  # only enabled/command/url per server


def test_liveness_passes_on_running_state_with_open_port(monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        monkeypatch.setattr(uv, "_read_gateway_state", lambda: {
            "gateway_state": "running",
            "platforms": {"webhook": {"listener_base": f"http://127.0.0.1:{port}"}},
        })
        ok, detail = uv.check_gateway_liveness(timeout=2.0)
        assert ok
        assert "accepting" in detail
    finally:
        listener.close()


def test_liveness_times_out_bounded(monkeypatch):
    monkeypatch.setattr(uv, "_read_gateway_state", lambda: {"gateway_state": "stopped"})
    import time
    started = time.monotonic()
    ok, _ = uv.check_gateway_liveness(timeout=1.0)
    assert not ok
    assert time.monotonic() - started < 10  # no infinite wait


def test_run_verification_skips_liveness_when_gateway_was_not_running(two_homes, monkeypatch):
    monkeypatch.setattr(uv, "check_gateway_liveness",
                        lambda *a, **k: pytest.fail("liveness must be skipped"))
    state = uv.capture_pre_state([("default", two_homes["root"])], backup=False)
    results = uv.run_verification(state)
    assert [c["name"] for c in results] == ["config_parity", "mcp_fingerprint_parity"]


def test_save_and_load_pre_state_roundtrip(two_homes):
    state = uv.capture_pre_state([("default", two_homes["root"])], backup=False)
    path = uv.save_pre_state(state)
    assert path.name == uv.PRE_STATE_FILENAME
    assert uv.load_pre_state(path) == state


class TestVerifyOrRollback:
    def _seed_repo(self, tmp_path):
        import subprocess as sp
        repo = tmp_path / "checkout"
        repo.mkdir()
        for argv in (["git", "init", "-q"], ["git", "config", "user.email", "t@example"],
                     ["git", "config", "user.name", "t"]):
            sp.run(argv, cwd=repo, check=True, capture_output=True)
        (repo / "a.txt").write_text("before\n", encoding="utf-8")
        sp.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        sp.run(["git", "commit", "-qm", "before"], cwd=repo, check=True, capture_output=True)
        sha = sp.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                     text=True, check=True).stdout.strip()
        (repo / "a.txt").write_text("after\n", encoding="utf-8")
        return repo, sha

    def test_failed_check_rolls_back_tree_and_configs(self, two_homes, tmp_path):
        repo, sha = self._seed_repo(tmp_path)
        state = uv.capture_pre_state([("default", two_homes["root"])], backup=True)
        state["pre_sha"] = sha
        (two_homes["root"] / "config.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")

        rc = uv.verify_or_rollback(state, checkout=repo)

        assert rc == 1
        assert (repo / "a.txt").read_text(encoding="utf-8") == "before\n"
        restored = yaml.safe_load((two_homes["root"] / "config.yaml").read_text(encoding="utf-8"))
        assert restored["mcp_servers"]["github"]["command"] == "npx"

    def test_clean_state_returns_zero_without_rollback(self, two_homes, tmp_path):
        repo, sha = self._seed_repo(tmp_path)
        state = uv.capture_pre_state([("default", two_homes["root"])], backup=True)
        state["pre_sha"] = sha

        assert uv.verify_or_rollback(state, checkout=repo) == 0
        assert (tmp_path / "checkout" / "a.txt").read_text(encoding="utf-8") == "after\n"

    def test_cli_entry_exits_nonzero_and_writes_receipt(self, two_homes, tmp_path):
        repo, sha = self._seed_repo(tmp_path)
        state = uv.capture_pre_state([("default", two_homes["root"])], backup=True)
        state["pre_sha"] = sha
        pre_state_file = tmp_path / "pre.json"
        pre_state_file.write_text(json.dumps(state), encoding="utf-8")
        (two_homes["root"] / "config.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")

        from hermes_cli.update_receipt import read_latest_receipt
        rc = uv.main(["--pre-state", str(pre_state_file), "--timeout", "1",
                      "--checkout", str(repo)])

        assert rc == 1
        receipt = read_latest_receipt()
        assert receipt is not None
        assert receipt["verification"]["rolled_back"] is True
        assert receipt["verification"]["failed_check"] == "config_parity"

    def test_capture_and_record_pre_state_survives_errors(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(uv, "capture_pre_state",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert uv.capture_and_record_pre_state() is None


class TestHeadShaResolvesHermesCheckout:
    """`pre_sha` must come from the Hermes checkout, never the caller's cwd.

    Regression: `_head_sha` ran `git rev-parse HEAD` with no `cwd=`, so a
    standalone verifier (or the completion child) launched from an unrelated
    directory recorded that repo's HEAD. `rollback_update` then ran
    `git reset --hard <unrelated-sha>` inside the Hermes checkout.
    """

    @staticmethod
    def _seed_repo(root, name, content):
        import subprocess as sp
        repo = root / name
        repo.mkdir(parents=True)
        sp.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        (repo / "a.txt").write_text(content, encoding="utf-8")
        sp.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        # Identity is explicit: this machine has no global user.email/user.name,
        # and a fresh tmp repo would otherwise fail the commit.
        sp.run(["git", "-c", "user.email=fixture@example.invalid",
                "-c", "user.name=Fixture", "commit", "-qm", name],
               cwd=repo, check=True, capture_output=True)
        return repo, sp.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                            text=True, check=True).stdout.strip()

    def test_head_sha_uses_the_given_checkout_not_the_process_cwd(self, tmp_path, monkeypatch):
        hermes, hermes_sha = self._seed_repo(tmp_path, "hermes-agent", "hermes\n")
        other, other_sha = self._seed_repo(tmp_path, "unrelated-repo", "other\n")
        assert hermes_sha != other_sha

        # Park the process cwd inside the UNRELATED repo — the old
        # no-cwd implementation would return other_sha here.
        monkeypatch.chdir(other)

        assert uv._head_sha(hermes) == hermes_sha
        assert uv._head_sha(hermes) != other_sha

    def test_head_sha_returns_empty_outside_a_repo(self, tmp_path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert uv._head_sha(plain) == ""


class TestRolledBackRequiresResetOk:
    """`rolled_back` must reflect the CODE moving back, not just config restore.

    Regression: `rolled_back = reset_ok or bool(restored_profiles)` reported
    success when config snapshots were restored but `git reset --hard` failed,
    so the receipt claimed a rollback that never happened.
    """

    def _state(self, home):
        state = uv.capture_pre_state([("default", home)], backup=True)
        state["pre_sha"] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        return state

    def test_config_restore_without_reset_reports_not_rolled_back(
            self, two_homes, tmp_path, monkeypatch):
        import subprocess as sp
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        sp.run(["git", "init", "-q"], cwd=checkout, check=True, capture_output=True)

        state = self._state(two_homes["root"])
        # Force a failing check so rollback runs.
        (two_homes["root"] / "config.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")

        recorded = {}
        monkeypatch.setattr(
            "hermes_cli.update_receipt.record_verification",
            lambda checks, rolled_back, failed_check: recorded.update(
                rolled_back=rolled_back, failed_check=failed_check))

        # pre_sha is absent from this repo, so `git reset --hard` must fail.
        uv.verify_or_rollback(state, checkout=checkout)

        assert recorded["failed_check"] == "config_parity"
        # Configs WERE restored (snapshot existed) — but the tree did not move.
        assert recorded["rolled_back"] is False, (
            "receipt claimed rolled_back=True although git reset --hard failed")

    def test_rollback_refuses_to_reset_with_an_empty_pre_sha(
            self, two_homes, tmp_path, monkeypatch):
        import subprocess as sp
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        sp.run(["git", "init", "-q"], cwd=checkout, check=True, capture_output=True)
        (checkout / "a.txt").write_text("x\n", encoding="utf-8")
        sp.run(["git", "add", "."], cwd=checkout, check=True, capture_output=True)
        sp.run(["git", "-c", "user.email=fixture@example.invalid",
                "-c", "user.name=Fixture", "commit", "-qm", "x"],
               cwd=checkout, check=True, capture_output=True)

        state = self._state(two_homes["root"])
        state["pre_sha"] = ""

        seen = {}

        def _spy(cmd, **kwargs):
            seen["cmd"] = cmd
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(uv.subprocess, "run", _spy)

        outcome = uv.rollback_update(state, checkout=checkout, pre_sha="",
                                    failed_check="config_parity")

        assert "reset" not in seen.get("cmd", []), "git reset was attempted with an empty SHA"
        assert outcome["reset_ok"] is False
        assert outcome["reset_detail"], "an empty pre_sha must leave an explanatory detail"
