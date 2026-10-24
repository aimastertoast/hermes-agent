"""The fork's Windows gateway relaunch surface — FORK-OWNED.

``hermes_cli/update_cmd_windows.py`` is upstream's and is merged on every update,
so the fork's Scheduled-Task relaunch (the escape from the updater's Job Object,
#48820) and its post-update gate live in the fork-owned
``hermes_cli/update_cmd_windows_fork.py``. These tests pin that behaviour so the
upstream file can stay a thin delegation.
"""

from __future__ import annotations

import pytest

from hermes_cli import gateway_windows
from hermes_cli import update_cmd_windows_fork as fork


class _FakeSchtasks:
    """Records the schtasks invocations and replays scripted results."""

    def __init__(self, results):
        self.calls: list[list[str]] = []
        self._results = list(results)

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        if self._results:
            return self._results.pop(0)
        return (0, "", "")


class TestScheduledTaskName:
    def test_default_profile_uses_the_bare_task_name(self):
        assert fork.scheduled_task_name_for_profile("default") == gateway_windows._TASK_NAME_DEFAULT

    def test_empty_profile_uses_the_bare_task_name(self):
        assert fork.scheduled_task_name_for_profile("") == gateway_windows._TASK_NAME_DEFAULT

    def test_named_profile_gets_its_own_task(self):
        assert fork.scheduled_task_name_for_profile("work") == f"{gateway_windows._TASK_NAME_DEFAULT}_work"


class TestRelaunchViaScheduledTask:
    def test_non_windows_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(fork.sys, "platform", "linux")
        fake = _FakeSchtasks([(0, "", "")])

        assert fork.relaunch_gateway_via_scheduled_task("default", 1) is False
        assert fake.calls == []

    def test_registered_task_is_queried_then_run(self, monkeypatch):
        fake = _FakeSchtasks([(0, "ok", ""), (0, "", "")])
        monkeypatch.setattr(fork.sys, "platform", "win32")
        monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake)

        assert fork.relaunch_gateway_via_scheduled_task("default", 1) is True
        assert fake.calls == [
            ["/Query", "/TN", gateway_windows._TASK_NAME_DEFAULT],
            ["/Run", "/TN", gateway_windows._TASK_NAME_DEFAULT],
        ]

    def test_missing_task_is_not_run(self, monkeypatch):
        fake = _FakeSchtasks([(1, "", "not found")])
        monkeypatch.setattr(fork.sys, "platform", "win32")
        monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake)

        assert fork.relaunch_gateway_via_scheduled_task("default", 1) is False
        assert [call[0] for call in fake.calls] == ["/Query"]

    def test_a_failing_run_reports_failure(self, monkeypatch):
        fake = _FakeSchtasks([(0, "ok", ""), (1, "", "access denied")])
        monkeypatch.setattr(fork.sys, "platform", "win32")
        monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake)

        assert fork.relaunch_gateway_via_scheduled_task("default", 1) is False

    def test_a_raising_schtasks_is_swallowed(self, monkeypatch):
        def boom(args, timeout=None):
            raise OSError("schtasks missing")

        monkeypatch.setattr(fork.sys, "platform", "win32")
        monkeypatch.setattr(gateway_windows, "_exec_schtasks", boom)

        assert fork.relaunch_gateway_via_scheduled_task("default", 1) is False


class TestRelaunchProfileGateway:
    def test_scheduled_task_wins_when_it_works(self, monkeypatch):
        monkeypatch.setattr(fork, "relaunch_gateway_via_scheduled_task", lambda p, o: True)
        calls = []
        monkeypatch.setattr(
            "hermes_cli.gateway.launch_detached_profile_gateway_restart",
            lambda p, o: calls.append((p, o)) or True,
        )

        assert fork.relaunch_profile_gateway("default", 42) is True
        assert calls == []

    def test_watcher_fallback_runs_when_the_task_is_missing(self, monkeypatch):
        monkeypatch.setattr(fork, "relaunch_gateway_via_scheduled_task", lambda p, o: False)
        calls = []
        monkeypatch.setattr(
            "hermes_cli.gateway.launch_detached_profile_gateway_restart",
            lambda p, o: calls.append((p, o)) or True,
        )

        assert fork.relaunch_profile_gateway("work", 42) is True
        assert calls == [("work", 42)]


class TestUpdateAppliedNewCodeGate:
    def test_the_gate_flips(self):
        original = fork.UPDATE_APPLIED_NEW_CODE
        try:
            fork._set_update_applied_new_code(True)
            assert fork.UPDATE_APPLIED_NEW_CODE is True
            fork._set_update_applied_new_code(False)
            assert fork.UPDATE_APPLIED_NEW_CODE is False
        finally:
            fork._set_update_applied_new_code(original)

    def test_the_gate_is_reachable_through_the_fork_module(self):
        """``update_cmd.py`` imports the setter from ``update_cmd_fork``; it must be the real one."""
        from hermes_cli import update_cmd_fork

        assert update_cmd_fork._set_update_applied_new_code is fork._set_update_applied_new_code
