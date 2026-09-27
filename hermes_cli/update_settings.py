"""User-facing update settings shared between the desktop GUI and CLI orchestrator.

The desktop renderer persists two safety toggles (``$updateAllowLocalAhead`` and
``$updateSafeModeAuto``) in electron-store. To make them reachable from the
Python orchestrator (which runs as a separate process spawned by the gateway),
an electron main-process IPC handler mirrors the current atom values to
``HERMES_HOME/update_settings.json`` whenever the renderer toggles them. The
Python side reads this file with :func:`load_update_settings` on every invoke.

Why a file rather than an HTTP param: the orchestrator is spawned detached by
the gateway's ``_spawn_hermes_action`` and runs to completion asynchronously.
Passing the toggle via env var works for the first hop but breaks the moment
the orchestrator's subprocess re-execs or restarts mid-pipeline. A file on
disk outlives the spawn and survives any subprocess handoff, at the cost of a
single fsync on each toggle flip from the desktop.

Schema is intentionally minimal — two booleans. Anything more elaborate
belongs in the desktop's electron-store; this file is only the cross-process
bridge.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


UPDATE_SETTINGS_FILENAME = "update_settings.json"
"""File name inside ``HERMES_HOME`` that holds the toggles."""

SCHEMA_VERSION = 1
"""Bumped on breaking format changes. Loader ignores files with newer schemas."""


@dataclass(frozen=True)
class UpdateSettings:
    """Toggle values mirrored from the desktop renderer.

    Defaults match the renderer's atom defaults (``persistentAtom(..., false)``)
    so a missing/corrupt file behaves like a fresh install.
    """

    allow_local_ahead: bool = False
    """``$updateAllowLocalAhead`` — bypass the LOCAL_AHEAD_REFUSAL gate so an
    update can proceed past the user's unpushed local commits. The orchestrator
    creates a backup branch first; the toggle only arms the bypass, it does not
    disable the safety net."""

    safe_mode_auto: bool = False
    """``$updateSafeModeAuto`` — let the orchestrator run ``hermes update``
    unattended when its pre/post classifier proves the change is safe. Default
    off so the user opts in once."""


def settings_path(home: Path) -> Path:
    """Resolve the on-disk path for the settings file.

    Centralized so tests can target the same location the desktop writes to.
    """
    return home / UPDATE_SETTINGS_FILENAME


def load_update_settings(home: Path) -> Optional[UpdateSettings]:
    """Read ``HERMES_HOME/update_settings.json`` and return parsed settings.

    Returns ``None`` when the file is missing, unreadable, or corrupt — never
    raises. The caller treats ``None`` as "use defaults"; we deliberately do
    not return a default-constructed ``UpdateSettings`` here so callers can
    distinguish "no file" from "file with all-default values" if they need to
    (e.g. to log first-launch telemetry).

    Honors ``HERMES_HOME`` via :func:`settings_path`; tests override the home
    by passing a tmp_path and monkeypatching the orchestrator's home argument.
    """
    path = settings_path(home)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("update_settings unreadable (%s): %s", path, exc)
        return None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.debug("update_settings corrupt (%s): %s", path, exc)
        return None

    if not isinstance(data, dict):
        logger.debug("update_settings not an object (%s)", path)
        return None

    schema = data.get("schema_version", 0)
    if not isinstance(schema, int) or schema > SCHEMA_VERSION:
        # Newer schema than we understand — fail closed (None) so the
        # orchestrator uses defaults instead of misreading future fields.
        logger.debug("update_settings schema %s > %s; ignoring", schema, SCHEMA_VERSION)
        return None

    toggles = data.get("toggles")
    if not isinstance(toggles, dict):
        return None

    allow_local_ahead = _read_bool(toggles, "allow_local_ahead")
    safe_mode_auto = _read_bool(toggles, "safe_mode_auto")
    if allow_local_ahead is None or safe_mode_auto is None:
        return None

    return UpdateSettings(
        allow_local_ahead=allow_local_ahead,
        safe_mode_auto=safe_mode_auto,
    )


def _read_bool(container: dict, key: str) -> Optional[bool]:
    """Strict boolean read — only accepts JSON true/false, never 0/1/\"true\".

    A loose coercion (e.g. ``bool(value)``) would silently treat a typo'd
    string like ``"false "`` as truthy and arm the local-ahead bypass.
    """
    value = container.get(key)
    if isinstance(value, bool):
        return value
    return None


def write_update_settings(home: Path, settings: UpdateSettings) -> None:
    """Atomically write the settings file. Used by tests and by future
    electron-main IPC; never call from the orchestrator (read-only).

    Atomic via the tmp+rename pattern so a half-written file can never be read
    by a concurrent orchestrator invoke.
    """
    path = settings_path(home)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "toggles": asdict(settings),
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        home.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.debug("update_settings write failed (%s): %s", path, exc)
        # Best-effort cleanup; do not mask the original write failure.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise