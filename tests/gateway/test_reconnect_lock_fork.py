"""The fork's cross-profile lock-conflict parking — FORK-OWNED.

``gateway/reconnect_lock_fork.py`` holds the behaviour and
``gateway/run_adapters.py`` keeps two thin delegations. A platform whose bot
credential is held by ANOTHER live profile gateway can never reconnect while
that holder lives (``--replace`` forbids eviction), and retrying every
30s..300s churns an adapter per attempt. The fork parks the platform on a slow
liveness-probe cadence and auto-recovers it once the holder's process exits.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Imported at COLLECTION time on purpose: gateway/run.py's bootstrap activates
# the payload dependencies on first import, and that reads the real Hermes home
# (the checkout lives inside it) — which the per-test home guard forbids. The
# fork module imports these helpers lazily, so patching them at call time works
# as long as the module is already in sys.modules.
import gateway.run  # imported for its module-load side effect
from gateway.reconnect_lock_fork import defer_lock_conflict, handle_lock_conflict

PLATFORM = SimpleNamespace(value="telegram")

LIVE_LOCK = "bot token is already in use by the telegram profile gateway"
STALE_LOCK = "bot token is already in use by another process"


def _adapter(code: str, message: str) -> SimpleNamespace:
    return SimpleNamespace(
        has_fatal_error=True,
        fatal_error_code=code,
        fatal_error_message=message,
    )


class TestDeferLockConflict:
    def test_live_holder_keeps_the_platform_parked(self):
        info = {"lock_conflict": True, "holder_pid": 4242, "next_retry": 0.0}

        with patch("gateway.run._pid_alive", return_value=True), patch(
            "gateway.run._LOCK_CONFLICT_RECHECK_SECONDS", 90
        ):
            assert defer_lock_conflict(info, PLATFORM, 1000.0) is True

        assert info["next_retry"] == 1090.0
        assert info["lock_conflict"] is True
        assert info["holder_pid"] == 4242

    def test_dead_holder_clears_the_markers_and_lets_the_retry_run(self):
        info = {"lock_conflict": True, "holder_pid": 4242, "next_retry": 0.0}

        with patch("gateway.run._pid_alive", return_value=False):
            assert defer_lock_conflict(info, PLATFORM, 1000.0) is False

        assert info["lock_conflict"] is False
        assert info["holder_pid"] is None


class TestHandleLockConflict:
    def test_live_cross_profile_conflict_is_parked(self):
        owner = MagicMock()
        info: dict = {}
        adapter = _adapter("telegram_lock", LIVE_LOCK)

        with patch("gateway.run._extract_lock_holder_pid", return_value=4242), patch(
            "gateway.run._LOCK_CONFLICT_RECHECK_SECONDS", 90
        ):
            assert handle_lock_conflict(owner, adapter, info, PLATFORM) is True

        owner._update_platform_runtime_status.assert_called_once()
        args, kwargs = owner._update_platform_runtime_status.call_args
        assert args[0] == "telegram"
        assert kwargs["platform_state"] == "needs_attention"
        assert kwargs["needs_attention"] is True
        assert info["lock_conflict"] is True
        assert info["holder_pid"] == 4242
        assert info["next_retry"] > 0

    def test_stale_holder_conflict_stays_retryable(self):
        owner = MagicMock()
        info: dict = {}

        assert handle_lock_conflict(owner, _adapter("telegram_lock", STALE_LOCK), info, PLATFORM) is False

        owner._update_platform_runtime_status.assert_not_called()
        assert "lock_conflict" not in info

    def test_other_failures_are_not_handled(self):
        owner = MagicMock()

        assert handle_lock_conflict(owner, _adapter("network_error", "refused"), {}, PLATFORM) is False

        owner._update_platform_runtime_status.assert_not_called()
