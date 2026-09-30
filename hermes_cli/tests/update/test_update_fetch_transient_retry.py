"""One flaky fetch must not abort the whole update.

``hermes update`` fetches once and, on any failure, prints a diagnosis and
``sys.exit(1)``. ``fetch_with_partial_clone_recovery`` only retries the git
2.53/2.54 pack-objects crash — a rate limit, an outage, a dropped connection or
our own 300 s transport timeout all end the run on the first try, so the user
has to click Update again. Retrying the transient classes (bounded, and with
visible progress so it never looks like a hang) is the fix; permanent causes
must still fail immediately with the diagnosis they already print.

The retry lives on the *runner* (``update_cmd._git_run(network=True)``), not on
the fetch call site, because upstream also rewrites those call lines — editing
them makes this branch merge-conflict with ``origin/main``, and a conflict stops
the update outright. ``TestGitRunWiring`` is what fails if that wiring is lost.
"""
from __future__ import annotations

from subprocess import CalledProcessError, CompletedProcess

import pytest

from hermes_cli import update_cmd

RATE_LIMIT_STDERR = (
    "error: RPC failed; HTTP 429 curl 22 The requested URL returned error: 429\n"
    "fatal: expected flush after ref listing"
)
HUNG_UP_STDERR = (
    "fatal: the remote end hung up unexpectedly\n"
    "error: unable to access"
)
AUTH_STDERR = (
    "fatal: Authentication failed for "
    "'https://github.com/NousResearch/hermes-agent.git/'"
)
PROMPT_STDERR = (
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
)
LOCAL_PATH_STDERR = (
    "fatal: unable to access 'C:/nowhere/': Could not read from remote repository\n"
    "fatal: does not appear to be a git repository"
)


def _fetch(rc, stderr=""):
    return CompletedProcess(["git", "fetch"], rc, stdout="", stderr=stderr)


def _retry(run, **kw):
    kw.setdefault("sleep", lambda _s: None)
    return update_cmd._with_transient_retry(run, **kw)


class _FakeMain:
    """Stand-in for ``hermes_cli.main``: only what ``_git_run`` reads, no import side effects."""

    def __init__(self, project_root):
        self.PROJECT_ROOT = project_root


class _Script:
    """Scripted zero-arg call: returns each prepared result in turn, counts calls."""

    def __init__(self, *results: CompletedProcess):
        self._results = list(results)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        index = min(self.calls - 1, len(self._results) - 1)
        return self._results[index]


class TestTransientClassifier:
    def test_rate_limit_is_transient(self):
        assert update_cmd.is_transient_fetch_failure(RATE_LIMIT_STDERR)

    def test_hung_up_connection_is_transient(self):
        assert update_cmd.is_transient_fetch_failure(HUNG_UP_STDERR)

    def test_our_own_transport_timeout_is_transient(self):
        assert update_cmd.is_transient_fetch_failure(
            "git fetch timed out after 300s (a stalled remote, or a transfer too large for the limit)"
        )

    def test_auth_failure_is_not_transient(self):
        assert not update_cmd.is_transient_fetch_failure(AUTH_STDERR)

    def test_anonymous_401_is_transient_because_the_diagnosis_says_try_again(self):
        # The classifier calls this a GitHub outage and tells the user to retry,
        # so the retry layer has to agree with the message it will print afterwards.
        assert update_cmd.is_transient_fetch_failure(PROMPT_STDERR)

    def test_pack_objects_crash_is_not_transient(self):
        # It has its own single dedicated recovery around this runner; the network
        # layer must not add a second one.
        assert not update_cmd.is_transient_fetch_failure(
            "BUG: builtin/pack-objects.c:4842: should_include_obj should only be called on existing objects\n"
            "error: pack-objects died of signal 6\n"
            "fatal: could not finish pack-objects to repack local links\n"
            "fatal: index-pack failed"
        )

    def test_a_local_path_mistake_is_permanent_not_transient(self):
        # It reaches us wrapped in "unable to access", which IS transient for a URL —
        # retrying this twice would only delay "your remote URL is wrong".
        assert not update_cmd.is_transient_fetch_failure(LOCAL_PATH_STDERR)


class TestRetry:
    def test_retries_a_transient_failure_and_returns_the_recovery(self):
        script = _Script(_fetch(124, HUNG_UP_STDERR), _fetch(0))
        sleeps: list[float] = []

        result = _retry(script, sleep=sleeps.append)

        assert script.calls == 2
        assert sleeps == [10]
        assert result.returncode == 0

    def test_stops_at_the_first_attempt_when_the_command_works(self):
        script = _Script(_fetch(0, ""))
        sleeps: list[float] = []

        _retry(script, sleep=sleeps.append)

        assert script.calls == 1
        assert sleeps == []

    def test_does_not_retry_a_failure_it_cannot_identify(self):
        # An unrecognised failure must fail at once: guessing "probably the network"
        # would hide a real error behind two pointless waits.
        script = _Script(_fetch(1, ""))
        sleeps: list[float] = []

        result = _retry(script, sleep=sleeps.append)

        assert script.calls == 1
        assert sleeps == []
        assert result.returncode == 1

    def test_does_not_retry_a_permanent_failure(self):
        script = _Script(_fetch(128, AUTH_STDERR))
        sleeps: list[float] = []

        result = _retry(script, sleep=sleeps.append)

        assert script.calls == 1, "retrying bad credentials only delays the diagnosis"
        assert sleeps == []
        assert result.returncode == 128

    def test_gives_up_after_the_budget_and_keeps_the_last_result(self):
        script = _Script(_fetch(1, RATE_LIMIT_STDERR))
        sleeps: list[float] = []

        result = _retry(script, sleep=sleeps.append)

        assert script.calls == 3, "default budget is the initial try plus two retries"
        assert sleeps == [10, 30]
        assert result.returncode == 1
        assert "429" in result.stderr

    def test_recovers_from_a_transient_failure_on_the_last_try(self):
        script = _Script(_fetch(1, RATE_LIMIT_STDERR), _fetch(1, RATE_LIMIT_STDERR), _fetch(0))
        sleeps: list[float] = []

        result = _retry(script, sleep=sleeps.append)

        assert result.returncode == 0
        assert script.calls == 3


class TestGitRunWiring:
    """The retry only helps if ``_git_run(network=True)`` actually applies it."""

    @pytest.fixture
    def scripted_git(self, monkeypatch, tmp_path):
        """Replace ``subprocess.run`` and the retry sleep; script the returned results.

        ``_m`` MUST be stubbed: resolving it imports ``hermes_cli.main``, which runs
        ``hermes_bootstrap.activate_dependencies`` against the real HERMES_HOME. Unstubbed
        this test has installed dependencies, rewritten ``install-stamp.json``, and left a
        ``source-completion-pending`` marker in the live install.
        """
        results: list[CompletedProcess] = []
        calls: list[list[str]] = []
        sleeps: list[float] = []

        def fake_run(cmd, **_kw):
            calls.append(list(cmd))
            index = min(len(calls) - 1, len(results) - 1)
            return results[index]

        monkeypatch.setattr(update_cmd, "_m", lambda: _FakeMain(tmp_path))
        monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)
        monkeypatch.setattr(update_cmd._time, "sleep", sleeps.append)
        return type("Script", (), {"results": results, "calls": calls, "sleeps": sleeps})()

    def test_a_network_command_retries_a_transient_failure(self, scripted_git):
        scripted_git.results += [_fetch(124, HUNG_UP_STDERR), _fetch(0)]

        result = update_cmd._git_run(["git"], ["fetch", "origin", "main"], network=True)

        assert len(scripted_git.calls) == 2, "the first failure must be retried, not fatal"
        assert scripted_git.sleeps == [10]
        assert result.returncode == 0

    def test_a_local_command_never_waits(self, scripted_git):
        scripted_git.results.append(_fetch(1, RATE_LIMIT_STDERR))

        result = update_cmd._git_run(["git"], ["status", "--porcelain"])

        assert len(scripted_git.calls) == 1, "local commands must not be retried"
        assert scripted_git.sleeps == []
        assert result.returncode == 1

    def test_check_still_raises_after_the_retries_are_spent(self, scripted_git):
        scripted_git.results.append(_fetch(128, AUTH_STDERR))

        with pytest.raises(CalledProcessError) as excinfo:
            update_cmd._git_run(["git"], ["fetch"], check=True, network=True)

        assert excinfo.value.returncode == 128
        assert len(scripted_git.calls) == 1
        assert "Authentication failed" in (excinfo.value.stderr or "")
