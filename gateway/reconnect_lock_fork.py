"""Fork-owned cross-profile lock-conflict handling for the adapter reconnect watcher.

``gateway/run_adapters.py`` is upstream-owned. A platform that lost the
credential lock to ANOTHER live profile gateway must NOT spin on the normal
30s..300s reconnect backoff forever: the retry can never succeed while the
holder lives (``--replace`` forbids eviction), and every attempt constructs an
adapter that then has to be disposed (fd churn). The fork parks such a platform
on a slow liveness-probe cadence instead, and auto-recovers it the moment the
holder's process exits.

The whole behaviour lives here; ``run_adapters._reconnect_failed_platform``
keeps only two thin delegations. Because the calls land INSIDE the module's own
method, monkeypatching these module attributes at call time reaches them.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("gateway.run")


def defer_lock_conflict(info: dict, platform: Any, now: float) -> bool:
    """Parked lock-conflict pass: True = defer the retry (the caller returns).

    While the holder's PID is alive the platform stays queued with a fresh
    slow-cadence re-arm — no adapter construction, no fd churn. When the
    holder exits, the deferral markers are cleared so the normal connect path
    runs and the platform recovers without manual intervention.
    """
    from gateway.run import _LOCK_CONFLICT_RECHECK_SECONDS, _pid_alive

    if _pid_alive(info.get("holder_pid")):
        info["next_retry"] = now + _LOCK_CONFLICT_RECHECK_SECONDS
        logger.info(
            "Reconnect %s: credential still held by profile gateway (PID %s), deferring %ds",
            platform.value,
            info.get("holder_pid"),
            _LOCK_CONFLICT_RECHECK_SECONDS,
        )
        return True
    logger.info(
        "Reconnect %s: lock holder (PID %s) exited — attempting auto-recovery now",
        platform.value,
        info.get("holder_pid"),
    )
    # Clear the deferral markers so a failure below reverts to normal backoff
    # handling; the branch above re-parks with a fresh holder PID if the lock
    # is contested again.
    info["lock_conflict"] = False
    info["holder_pid"] = None
    return False


def handle_lock_conflict(owner: Any, adapter: Any, info: dict, platform: Any) -> bool:
    """Failed-attempt pass: True = the attempt was a live cross-profile lock
    conflict and is now parked (the caller stops processing it).

    A stale-holder lock failure keeps its retryable behaviour: its message has
    no "profile gateway" attribution, so the predicate does not match.
    """
    from gateway.run import (
        _LOCK_CONFLICT_RECHECK_SECONDS,
        _extract_lock_holder_pid,
    )

    if not (
        adapter.has_fatal_error
        and (adapter.fatal_error_code or "").endswith("_lock")
        and "already in use by the" in (adapter.fatal_error_message or "")
        and "profile gateway" in (adapter.fatal_error_message or "")
    ):
        return False

    owner._update_platform_runtime_status(
        platform.value,
        platform_state="needs_attention",
        error_code=adapter.fatal_error_code,
        error_message=adapter.fatal_error_message,
        needs_attention=True,
    )
    holder_pid = _extract_lock_holder_pid(adapter.fatal_error_message or "")
    info["lock_conflict"] = True
    info["holder_pid"] = holder_pid
    info["next_retry"] = time.monotonic() + _LOCK_CONFLICT_RECHECK_SECONDS
    logger.warning(
        "%s credential held by another profile gateway (PID %s) — will "
        "auto-recover when it exits (liveness re-check every %ds)",
        platform.value,
        holder_pid,
        _LOCK_CONFLICT_RECHECK_SECONDS,
    )
    return True
