"""Bound the LOCAL git calls of the update path — FORK-OWNED.

``git status --porcelain``, ``git rev-list --count``, ``git config --get-regexp`` and
``git cherry`` are LOCAL commands, but on a treeless partial clone they read trees that
git fetches from the promisor remote — and git spawns that lazy fetch with no timeout of
its own. The update then sits there with no output: observed at 98 minutes with zero
output after preflight, and upstream as 332 packs / 180 GiB over 7 h (#131444).

The bound lives HERE, in a file upstream has never heard of, and is installed into
``hermes_cli.update_cmd_git`` at import time. Upstream's own ``_git_run`` stays
byte-identical: the fork wraps the runner instead of rewriting it, so the fork's delta on
that upstream-owned file stays a pure insertion (a rewritten line is a merge-conflict
magnet, and a conflict stops the update outright).

Two pieces:

* ``hermes_cli.update_custody.run_git`` is wrapped once. The wrapper applies the bound and
  turns a ``TimeoutExpired`` into a FAILED RUN (exit 124) instead of an exception, so
  every caller keeps its single error path.
* ``update_cmd_git._git_run`` is rebound to accept ``timeout=`` (upstream's takes no such
  parameter) and to pick the default bound for the local commands above. The value travels
  to the runner in a ContextVar, because upstream's ``_git_run`` does not forward a timeout
  to ``run_git``.
"""

from __future__ import annotations

import subprocess
from contextvars import ContextVar
from typing import Any, Optional, Sequence

# ``status``/``cherry``/``rev-list``/``config`` are local calls that normally finish in well
# under a second; this is pure backstop so none of them can hold the update open
# indefinitely.
LOCAL_GIT_TIMEOUT_SECONDS = 120

# The bounded set: the local commands the parked-branch guard runs. They read trees/objects
# and are therefore the ones that can trigger a promisor lazy-fetch.
_BOUNDED_COMMANDS = (
    ("status", "--porcelain"),
    ("rev-list", "--count"),
    ("config", "--get-regexp"),
    ("cherry",),
)

# The bound the current call carries. Set by the ``_git_run`` rebind around the call to
# upstream's ``_git_run``; unset everywhere else, so no other caller's git spawn changes.
# ContextVar, not a global: the bound follows the task, so a concurrent call in another
# thread or task never inherits it.
_pending_timeout: ContextVar[Optional[int]] = ContextVar(
    "hermes_bounded_git_timeout", default=None)


def _default_timeout_for(args: Sequence[Any]) -> Optional[int]:
    """The default bound for ``args``, or None when the command is not a bounded one."""
    argv = tuple(str(arg) for arg in args)
    for prefix in _BOUNDED_COMMANDS:
        if argv[: len(prefix)] == prefix:
            return LOCAL_GIT_TIMEOUT_SECONDS
    return None


def _install_runner() -> None:
    """Wrap ``update_custody.run_git`` so a bounded call cannot hang the update.

    The wrapper is global and permanent once installed: every call site that reaches
    ``run_git`` through the module (``from hermes_cli.update_custody import run_git``
    inside a function body) gets it. A call site that bound the name at import time,
    before this ran, keeps the unwrapped runner -- which is why the bound is applied
    through the ContextVar below rather than by the wrapper alone.
    """
    from hermes_cli import update_custody

    if getattr(update_custody.run_git, "_hermes_bounded", False):
        return
    base_run_git = update_custody.run_git

    def run_git(git_cmd, args, **kwargs):
        timeout = _pending_timeout.get()
        if timeout is None:
            # No bound on this call: hand it to the real runner untouched, so a caller that
            # passes its own timeout keeps its own error handling.
            return base_run_git(git_cmd, args, **kwargs)
        kwargs = {**kwargs, "timeout": timeout}
        try:
            return base_run_git(git_cmd, args, **kwargs)
        except subprocess.TimeoutExpired as exc:
            # The child is already killed; report it as a failed run (exit 124) rather than an
            # exception, so every caller's one stderr path also covers a git that never came back.
            argv = [str(part) for part in git_cmd] + [str(arg) for arg in args]
            stderr = f"git {args[0] if args else ''} timed out after {timeout}s"
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(
                    124, argv, output="", stderr=stderr) from exc
            return subprocess.CompletedProcess(argv, 124, stdout="", stderr=stderr)

    run_git._hermes_bounded = True  # type: ignore[attr-defined]
    update_custody.run_git = run_git


def install_bounded_git_timeout(namespace: dict) -> None:
    """Bound the local git calls of the module whose globals are ``namespace``.

    ``namespace`` must carry the module's ``_git_run``: it is rebound to accept
    ``timeout=`` and to apply the default bound to the local commands. Upstream's
    implementation is kept as the base, so its behaviour (custody, utf-8 decoding, the
    hidden console window) stays exactly as upstream wrote it.
    """
    _install_runner()
    base_git_run = namespace["_git_run"]

    def _git_run(git_cmd, args, cwd=None, *, check=False, env=None, timeout=None):
        if timeout is None:
            timeout = _default_timeout_for(args)
        token = _pending_timeout.set(timeout)
        try:
            return base_git_run(git_cmd, args, cwd=cwd, check=check, env=env)
        finally:
            _pending_timeout.reset(token)

    _git_run.__doc__ = (base_git_run.__doc__ or "") + (
        "\n\n    FORK: ``timeout=`` bounds the call (upstream's takes no such parameter), and the"
        "\n    local commands carry a default bound -- see hermes_cli/update_git_timeout.py."
    )
    namespace["_git_run"] = _git_run
