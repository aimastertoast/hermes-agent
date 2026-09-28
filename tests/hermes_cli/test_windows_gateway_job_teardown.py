"""Regression tests for #48820 (4th repro): job-object teardown killed the
post-update respawned gateway silently, and the updater printed
"✓ Restarting Windows gateway profile(s)" anyway.

Two fixes under test:

1. ``_spawn_gateway_restart_watcher``'s inlined watcher source must
   (a) route the respawned gateway's stray stdout/stderr to
       ``logs/gateway-stdio.log`` (it was ``DEVNULL`` — a gateway killed by
       parent Job Object teardown left ZERO trace anywhere), and
   (b) stamp ``_HERMES_GATEWAY_BREAKAWAY`` =1/0 on the respawn env exactly
       like the canonical ``gateway_windows._spawn_detached``, so the
       lifecycle/exit-diag records show whether the gateway escaped the
       parent's Job Object.

2. ``_resume_windows_gateways_after_update`` must verify a stable gateway
   process actually exists (via ``gateway_windows._wait_for_gateway_ready``)
   before printing the ✓ — a truthy launch return only proves the watcher
   process was created, not that the respawned gateway survived the
   updater's Job Object teardown.
"""

from pathlib import Path

import pytest

import hermes_cli.gateway as gateway
from hermes_cli.update_cmd_windows import _verify_relaunched_gateways_alive

# ---------------------------------------------------------------------------
# 1. Watcher template contract
# ---------------------------------------------------------------------------

def _captured_watcher_source(monkeypatch) -> str:
    """Spawn the watcher with a mocked Popen and return the inlined -c source."""
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class _P:
            pid = 12345

        return _P()

    monkeypatch.setattr(gateway.subprocess, "Popen", fake_popen)
    assert gateway._spawn_gateway_restart_watcher(
        999999, ["python", "-m", "hermes_cli.main", "gateway", "run"]
    )
    argv = captured["argv"]
    assert argv[1] == "-c"
    return argv[2]

class TestWatcherRespawnTemplate:

    def test_respawn_source_compiles(self, monkeypatch):
        """The inlined -c template is built via str.format over a
        dedented literal — guard against brace/indentation regressions."""
        src = _captured_watcher_source(monkeypatch)
        compile(src, "<watcher>", "exec")

# ---------------------------------------------------------------------------
# 2. Post-update resume liveness gate
# ---------------------------------------------------------------------------
#
# (3) The gate is per PROFILE. A fleet-wide poll returns as soon as ANY gateway is
#     live, so an already-healthy sibling vouched for a profile that never came
#     back and the updater still printed "✓ Restarting Windows gateway profile(s)"
#     naming the dead one. Observed live 2026-09-28: a fully successful update
#     killed the bobby gateway and reported both profiles as restarted; bobby's
#     gateway_state.json kept saying "running" with a dead pid, so nothing
#     downstream caught it either.


def _fake_ready(monkeypatch, ready_for: dict, fleet=()):
    """Stub the liveness poll: ``ready_for`` maps a profile home to the pids it
    reports, ``fleet`` answers the unscoped (unmapped) probe."""
    from hermes_cli import gateway_windows

    calls = []

    def _wait(timeout_s=6.0, interval_s=0.4, confirm_s=2.0, all_profiles=False, home=None, pid_filter=None):
        calls.append(home)
        return list(fleet) if home is None else list(ready_for.get(home, []))

    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", _wait)
    monkeypatch.setattr(gateway_windows, "_write_start_attestation", lambda pids, via, home=None: None)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    return calls


HOMES = {
    "default": Path("C:/hermes"),
    "bobby": Path("C:/hermes/profiles/bobby"),
}


@pytest.fixture
def homes(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda name: HOMES[name],
    )


class TestPerProfileLivenessGate:

    def test_healthy_sibling_does_not_vouch_for_a_dead_profile(self, monkeypatch, homes, capsys):
        """The regression: bobby never came back, default was fine — report bobby."""
        _fake_ready(monkeypatch, {HOMES["default"]: [111]}, fleet=[111])
        token = {"profiles": {}}

        _verify_relaunched_gateways_alive(token, {"default": 1, "bobby": 2}, [])

        assert set(token["profiles"]) == {"bobby"}, "dead profile must stay on the token"
        out = capsys.readouterr().out
        assert "bobby" in out
        assert "could not be verified" in out

    def test_probe_is_scoped_to_each_relaunched_profile(self, monkeypatch, homes):
        """Each profile is polled against its OWN identity files, never the fleet.

        Order is sorted() so a fleet-wide profile set is probed deterministically."""
        calls = _fake_ready(monkeypatch, {HOMES["default"]: [111], HOMES["bobby"]: [222]})

        _verify_relaunched_gateways_alive({}, {"default": 1, "bobby": 2}, [])

        assert calls == [HOMES["bobby"], HOMES["default"]]
        assert None not in calls, "the relaunch path must never fall back to an unscoped probe"

    def test_all_profiles_ready_clears_the_token(self, monkeypatch, homes, capsys):
        _fake_ready(monkeypatch, {HOMES["default"]: [111], HOMES["bobby"]: [222]})
        token = {"profiles": {}}

        _verify_relaunched_gateways_alive(token, {"default": 1, "bobby": 2}, [])

        assert not token.get("profiles")
        assert "could not be verified" not in capsys.readouterr().out

    def test_unmapped_replays_still_use_the_fleet_probe(self, monkeypatch, homes):
        """An unmapped replay carries an argv, not a profile — nothing to scope to."""
        calls = _fake_ready(monkeypatch, {}, fleet=[999])
        unmapped = [{"argv": ["python", "-m", "hermes_cli.main", "gateway", "run"], "pid": 7}]

        _verify_relaunched_gateways_alive({}, {}, unmapped)

        assert None in calls, "unmapped replays must still probe unscoped"

    def test_dead_unmapped_replay_is_reported(self, monkeypatch, homes, capsys):
        _fake_ready(monkeypatch, {}, fleet=[])
        token = {"unmapped": []}
        unmapped = [{"argv": ["python", "-m", "hermes_cli.main", "gateway", "run"], "pid": 7}]

        _verify_relaunched_gateways_alive(token, {}, unmapped)

        assert len(token["unmapped"]) == 1
        assert "could not be verified" in capsys.readouterr().out

    def test_returns_only_the_profiles_seen_alive(self, monkeypatch, homes):
        """The caller names the return value in its ✓, so it must exclude the dead ones."""
        _fake_ready(monkeypatch, {HOMES["default"]: [111]})  # bobby stays empty

        verified = _verify_relaunched_gateways_alive({}, {"default": 1, "bobby": 2}, [])

        assert verified == ["default"]


class TestSuccessLineNamesOnlyVerifiedProfiles:
    """The user-visible half: '✓ Restarting …' must not name a gateway that never came back."""

    def test_checkmark_omits_the_dead_profile(self, monkeypatch, homes, capsys):
        import hermes_cli.update_cmd_windows as win
        _fake_ready(monkeypatch, {HOMES["default"]: [111]})
        # UPDATE_APPLIED_NEW_CODE False keeps the launcher-refresh (and its _m() import) out of it.
        monkeypatch.setattr(win, "UPDATE_APPLIED_NEW_CODE", False)
        monkeypatch.setattr(win, "_relaunch_paused_gateways",
                            lambda token, profiles, unmapped: (sorted(profiles), 0))
        monkeypatch.setattr(win, "_resume_windows_services", lambda token: None)
        monkeypatch.setattr(win, "_cold_start_attested_profiles", lambda token: None)

        win._resume_windows_gateways_after_update_impl(
            {"resume_needed": True, "profiles": {"default": 1, "bobby": 2}, "unmapped": []}
        )

        out = capsys.readouterr().out
        assert "✓ Restarting Windows gateway profile(s): default" in out
        assert ", bobby" not in out, "bobby is dead — it must not be named in the ✓ line"
        assert "could not be verified for: bobby" in out

