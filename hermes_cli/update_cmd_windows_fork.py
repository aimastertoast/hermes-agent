"""Fork-owned Windows gateway relaunch surface for the update pipeline.

``hermes_cli/update_cmd_windows.py`` is upstream-owned and merged on every
update, so the fork's Windows-specific relaunch machinery lives here. Two
behaviours:

1. ``UPDATE_APPLIED_NEW_CODE`` / ``_set_update_applied_new_code`` — a module-
   level gate the update flow flips after a successful fetch. It is re-exported
   through ``update_cmd_fork`` (which copies ``update_cmd_windows``'s public
   surface) so ``update_cmd.py``'s existing import keeps working.

   NOTE: the gate was historically consulted by ``_resume_paused_set`` to
   decide whether to run the post-update launcher refresh / cold-start. An
   upstream merge rewrote that function and dropped the consultation, so the
   gate is currently DEAD (set but never read). It is kept because removing
   the call site in ``update_cmd.py`` would enlarge that file's fork delta, and
   the set is cheap. If a future fork change re-introduces a read, the gate is
   already here.

2. ``relaunch_profile_gateway`` — relaunch a paused profile gateway through
   its registered Scheduled Task (spawns outside the updater's Job Object, the
   only reliable escape when the parent job denies
   ``CREATE_BREAKAWAY_FROM_JOB``), falling back to the watcher-based restart
   for missing tasks. ``_relaunch_paused_gateways`` delegates here so its body
   stays upstream's bytes.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger("hermes_cli.update_cmd")

# Gate for the atexit resume callback (Defect B ordering fix): True only after the update flow's
# fetch succeeded (checkout about to move / update completing normally). While False (failed fetch,
# pre-fetch abort) the resume callback only restores gateways THIS updater paused - job-proof via
# the Scheduled Task - and skips the post-update launcher refresh / cold-start handling, so a
# transient GitHub 429 "touches nothing else" and never recreates the #48820 Job-Object kill window.
UPDATE_APPLIED_NEW_CODE = False


def _set_update_applied_new_code(value: bool) -> None:
    """Flip :data:`UPDATE_APPLIED_NEW_CODE` from the update flow (module-level so atexit sees it)."""
    global UPDATE_APPLIED_NEW_CODE
    UPDATE_APPLIED_NEW_CODE = bool(value)


def scheduled_task_name_for_profile(profile: str) -> str:
    """Task-Scheduler task name for a profile gateway (default: ``Hermes_Gateway``)."""
    from hermes_cli import gateway_windows
    base = gateway_windows._TASK_NAME_DEFAULT
    return base if (not profile or profile == "default") else f"{base}_{profile}"


def relaunch_gateway_via_scheduled_task(profile: str, old_pid: int) -> bool:
    """Relaunch a profile gateway through its registered Scheduled Task; True on success.

    ``schtasks /run`` hands the launch to Task Scheduler, which spawns the gateway OUTSIDE the
    updater's Job Object - the only reliable escape when the parent job denies
    CREATE_BREAKAWAY_FROM_JOB (the #48820 teardown kill). The task is the same durable path the
    gateway already uses at login/reboot (``Hermes_Gateway.cmd``). Returns False when the task is
    not registered or /run fails, so the caller can fall back to an in-process spawn.
    """
    if sys.platform != "win32":
        return False
    from hermes_cli import gateway_windows
    task_name = scheduled_task_name_for_profile(profile)
    try:
        code, _out, _err = gateway_windows._exec_schtasks(["/Query", "/TN", task_name])
    except Exception as exc:
        logger.debug("Could not query scheduled task %s: %s", task_name, exc)
        return False
    if code != 0:
        logger.debug("Scheduled task %s not registered; falling back to direct spawn", task_name)
        return False
    try:
        code, out, err = gateway_windows._exec_schtasks(["/Run", "/TN", task_name])
    except Exception as exc:
        logger.warning("schtasks /run %s raised: %s", task_name, exc)
        return False
    if code != 0:
        logger.warning("schtasks /run %s failed (code %s): %s", task_name, code, (err or out or "").strip())
        return False
    logger.info("Relaunched Windows gateway profile %s via scheduled task %s", profile, task_name)
    return True


def relaunch_profile_gateway(profile: str, old_pid: int) -> bool:
    """Relaunch a paused profile gateway: Scheduled Task first, watcher fallback second.

    The Scheduled Task spawns outside the updater's Job Object so the gateway survives
    updater teardown (#48820; the updater's job denies breakaway). Fall back to the
    watcher-based restart (tries CREATE_BREAKAWAY_FROM_JOB) for missing tasks.
    """
    from hermes_cli.gateway import launch_detached_profile_gateway_restart
    return relaunch_gateway_via_scheduled_task(str(profile), int(old_pid)) or \
        launch_detached_profile_gateway_restart(str(profile), int(old_pid))
