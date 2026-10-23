"""FORK: a command the Desktop spawns for itself leaves an owed source-update tail alone.

The completion tail (product builds + post-build maintenance) runs under the update lock.
The lock's marker is what the gateway watches, and a live marker fences the gateway
(``update_paused``). The Desktop's own commands travel the same launch path as a user's
shell, so they used to run the tail inline -- and the tail outlived the Desktop's own
timeouts:

    02:39:21  hermes gateway ensure produced no result (exit 7): ... timed out
    02:40:44  Desktop boot failed: hermes gateway ensure produced no result (exit 7)
    02:41:25  Gateway draining (update_paused). Use hermes gateway restart for recovery.
    02:41:26  update in progress (marker); deferring backend start until it finishes

The ``gateway ensure`` the Desktop spawned ran the owed tail inline; the tail took the
update lock, so the marker went live, the gateway the Desktop was starting fenced
itself, and the Desktop's boot gate then parked on the marker its own boot had created.
The repair meant to fix the install was the thing that broke it.

Upstream already exempts the Desktop's recovery diagnostic (``gateway status``) from the
inline tail for exactly this reason. These are the rest of the Desktop's own commands:
the backend boot, the owner restart, the gateway stop the update runs first, the
read-only pre-update config read, the read-only verifications the update hand-off runs,
the update check, and the state.db pre-flight snapshot. The tail belongs to ``hermes
update``: a Desktop-spawned command leaves it owed, and the launch says so where an
operator will read it.

Two of those are the update's own front door: the pre-flight snapshot runs the tail with a
30 second timeout, so an inline tail cancels the update it was meant to protect, and the
pre-update gateway stop takes the update lock the update itself is about to take.
"""

from __future__ import annotations

#: The Desktop's own commands, as ``hermes_cli._parser.command_argv`` returns them. Each one
#: boots or judges the very gateway an inline tail would fence, or reads state the Desktop
#: needs while an update is owed.
_OWNS_TAIL_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("gateway", "ensure"),
    ("gateway", "restart"),
    ("gateway", "stop"),
    ("config", "get"),
    ("hermes_cli.desktop_update_verify",),
    ("hermes_cli.update_verification",),
    ("hermes_cli.source_check",),
    ("hermes_cli.source_check_fork",),
    ("hermes_cli.backup_sqlite",),
)


def desktop_owes_tail(command: list[str]) -> bool:
    """Whether *command* must leave an owed source-update tail to ``hermes update``."""
    return any(tuple(command[:len(prefix)]) == prefix for prefix in _OWNS_TAIL_COMMANDS)
