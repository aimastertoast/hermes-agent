"""The fork's update checker at the two CLI call sites that reach it.

``hermes_cli/banner.py`` (the startup banner) and ``hermes_cli/_startup_fast.py``
(``--version``) are upstream-owned, and the fork's line in each is a PURE
INSERTION: the fork installs its own ``_check_branch`` for the duration of the
call instead of replacing the call, so upstream's own line stays byte-identical
and a routine upstream merge has nothing to collide with.

These tests pin what that insertion buys: the call reaches the fork's branch
check, so a local-only branch is measured against main instead of being
reported as the "couldn't reach the update server" the Desktop renders.
"""
from __future__ import annotations

from hermes_cli import source_check
from hermes_cli import source_check_fork


def _record_branch_check(monkeypatch) -> list:
    """Record the ``_check_branch`` upstream's own check sees while it runs."""
    seen: list = []

    def _fake_check(**kwargs):
        seen.append(source_check._check_branch)
        return {"behind": 0}

    monkeypatch.setattr(source_check, "check_for_updates", _fake_check)
    return seen


def test_the_banner_prefetch_checks_through_the_fork(monkeypatch):
    from hermes_cli import banner

    seen = _record_branch_check(monkeypatch)
    # The prefetch stands down under pytest (its daemon threads shell out to
    # git); here it must run, on this thread, so the check is observable.
    monkeypatch.setattr(banner, "_skip_background_prefetch", lambda: False)
    monkeypatch.setattr(banner, "_daemon", lambda name, target: target())

    banner.prefetch_update_check()

    assert seen == [source_check_fork._check_branch]


def test_the_version_line_checks_through_the_fork(monkeypatch, capsys):
    from hermes_cli import _startup_fast

    seen = _record_branch_check(monkeypatch)

    _startup_fast.print_fast_version_info(check_updates=True)

    assert seen == [source_check_fork._check_branch]
    assert "Up to date" in capsys.readouterr().out


def test_the_fork_swap_is_released_after_the_call(monkeypatch):
    """The swap is process-wide while it lasts, so it must not outlive the call."""
    from hermes_cli import _startup_fast

    upstream = source_check._check_branch
    _record_branch_check(monkeypatch)

    _startup_fast.print_fast_version_info(check_updates=True)

    assert source_check._check_branch is upstream
