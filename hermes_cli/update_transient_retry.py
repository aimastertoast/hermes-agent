"""Transient-failure classification and bounded retry for network git — FORK-OWNED.

``hermes update`` fetches once and, on any failure, prints a diagnosis and exits 1. A rate
limit, an outage, a dropped socket or the updater's own 300 s transport timeout all end the
run on the first try, so the user has to click Update again. Retrying the transient classes
(bounded, and with visible progress so it never looks like a hang) is the fix; permanent
causes must still fail immediately with the diagnosis they already print.

This lives in its own fork-owned module rather than in ``hermes_cli/update_cmd_git.py``:
that file is upstream-owned and is merged on every update, so fork code parked in it is a
merge-conflict magnet — a conflict stops the update outright. A file upstream has never
heard of cannot conflict.
"""

from __future__ import annotations

import time

# Every transient class below used to end `hermes update` on the first fetch attempt, so one
# blip cost the user another click. The delays are the whole budget: one sleep per entry, so
# ``len(delays) + 1`` attempts, each announced before it sleeps (a silent wait is the bug we
# just spent 98 minutes diagnosing).
FETCH_RETRY_DELAYS_S = (10, 30)

# Checked first: retrying these only delays the diagnosis the caller is about to print.
_PERMANENT_FETCH_MARKERS = (
    "Authentication failed",
    "Permission denied (publickey)",
    "Host key verification failed",
    "Repository not found",
    "not a git repository",
    # Both reach us wrapped in "unable to access '<x>'" below, which IS transient for a URL —
    # without these a misconfigured local path would be retried twice and then reported as a
    # network outage.
    "does not appear to be a git repository",
    "No such file or directory",
)

_TRANSIENT_FETCH_MARKERS = (
    "Could not resolve host",
    "unable to access",
    "Connection timed out",
    "Operation timed out",
    "Failed to connect",
    "Could not connect to server",
    "timed out",
    "the remote end hung up unexpectedly",
    "early EOF",
    "RPC failed",
    "SSL_ERROR",
    # Matches _classify_fetch_failure, which calls this a GitHub outage and tells the
    # user to try again in a few minutes — the retry layer must agree with its own message.
    "could not read Username",
)


def is_transient_fetch_failure(stderr: str) -> bool:
    """True when a later fetch attempt could plausibly succeed (rate limit, outage, network)."""
    from hermes_cli.update_cmd_git import _has_http_code

    text = stderr or ""
    if any(marker in text for marker in _PERMANENT_FETCH_MARKERS):
        return False
    if _has_http_code(text, "429", "500", "502", "503", "504") or "rate limit" in text.lower():
        return True
    return any(marker in text for marker in _TRANSIENT_FETCH_MARKERS)


def with_transient_retry(run, *, delays=FETCH_RETRY_DELAYS_S, sleep=None, label="fetch"):
    """Run ``run()`` up to ``1 + len(delays)`` times while its failure looks transient.

    ``run() -> CompletedProcess``. The wait is printed *before* sleeping, so a retry never
    reads as a hang, and a permanent cause (auth, not-found, bad local path) returns on the
    first attempt with no wasted wait. Always returns the last result whatever its exit
    code, so the caller's failure handling is untouched.

    This wraps the git *runner*, not the fetch, so ``fetch_with_partial_clone_recovery``'s
    pack-objects recovery still wraps every attempt: a git 2.53/2.54 crash (#124272) and a
    dropped socket are different failures, and neither one double-retries the other.

    ``sleep`` resolves at call time, not at def time — a default of ``time.sleep`` would
    capture the function object and no test could substitute a no-op for it.
    """
    nap = time.sleep if sleep is None else sleep
    result = run()
    for delay in delays:
        if result.returncode == 0 or not is_transient_fetch_failure(getattr(result, "stderr", "") or ""):
            return result
        print(f"  ⚠ {label} failed — retrying in {delay}s…")
        nap(delay)
        result = run()
    return result
