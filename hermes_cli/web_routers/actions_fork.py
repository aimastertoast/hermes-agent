"""The fork's dashboard update routes — FORK-OWNED.

``hermes_cli/web_routers/actions.py`` is one of the files upstream rewrites most
(36 of the last 300 upstream commits touch it), and the fork's changes to it were
edits to upstream's OWN lines — the shape a routine upstream merge collides with. A
collision ABORTS ``hermes update`` (the Desktop's "Update failed (exit 1)" banner),
so the fork's behavior moved here and upstream's file is byte-identical to upstream
again.

This router SHADOWS upstream's: it is mounted BEFORE ``_actions_routes.router`` in
``hermes_cli/web_server.py``, and FastAPI resolves a path to the FIRST registered
route — so these four routes win the paths they share with upstream:

===========================================  ==================================
this router (fork)                          upstream (``actions.py``)
===========================================  ==================================
``POST /api/hermes/update``                  same path, upstream's handler
``GET  /api/hermes/update/check``           same path, upstream's handler
``GET  /api/hermes/update/receipt``         same path, upstream's handler
``POST /api/hermes/update/receipt/{id}/ack``  fork-only route
===========================================  ==================================

The fork's behavior is reached WITHOUT editing upstream's file, by swapping module
globals for the duration of one call (upstream's handlers look their helpers up as
module globals at call time, and their local imports resolve from the module at
call time too):

* ``update_contract.evaluate_update_admission`` — the fork's force-requirement gate
  runs right after upstream's admission check, so a docker/apt refusal still wins.
* ``actions._UPDATE_REFUSAL_ERROR_CODES`` — the fork's own refusal codes
  (``local-ahead``, ``force-pushed``) are absent from upstream's map, which would
  flatten them to the generic ``update_not_in_place`` the Desktop cannot explain.
* ``actions._read_latest_receipt`` — the fork reads BOTH receipt stores (the
  pipeline record and upstream's legacy archive), so upstream's own summary builder
  summarises the very receipt the fork serves.
* ``source_check._check_branch`` — via :func:`source_check_fork.fork_branch_check`,
  so a fork-local branch is measured against main instead of reported as an error.

Each swap is scoped to one call and restored in a ``finally``. The windows hold no
suspension point that can interleave a second request's swap (upstream's update
handler has no ``await``), so a concurrent request cannot see a half-swapped module.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, StrictBool

from hermes_cli import source_check_fork
from hermes_cli import update_contract
from hermes_cli.web_deps import late
from hermes_cli.web_routers import actions as _actions

router = APIRouter()

_config_profile_scope = late("_config_profile_scope", "hermes_cli.web_server_profiles")

#: The fork's refusal codes, mapped to themselves: upstream's handler asks
#: ``_UPDATE_REFUSAL_ERROR_CODES`` for the code it reports, and a code the map does
#: not know becomes the generic ``update_not_in_place`` — a refusal the Desktop
#: cannot explain and the user cannot act on.
_FORK_REFUSAL_ERROR_CODES = {
    "local-ahead": "local-ahead",
    "force-pushed": "force-pushed",
}


def _fork_home(project_root: Path) -> Path:
    """The home the force-requirement gate reads its toggle from."""
    return Path(os.environ.get("HERMES_HOME") or project_root.parent)


@contextlib.contextmanager
def _force_requirement_gate():
    """The fork's force-requirement gate, in upstream's place.

    Upstream's ``update_hermes()`` refuses an update it cannot apply in place
    (docker, nix, apt, a commit build). The fork refuses one more case: the
    install is AHEAD of upstream (or upstream was force-pushed), so an in-place
    update would move the user off their own commits. The gate ran inside
    upstream's handler; it runs here instead, so upstream's file stays untouched.

    The gate is inserted by swapping TWO module globals for the duration of the
    call:

    * ``update_contract.evaluate_update_admission`` — upstream's handler imports it
      lazily, so the swap reaches it. The wrapper runs upstream's admission check
      FIRST (a docker/apt refusal is the more fundamental one and must win) and
      only then the fork's force requirement.
    * ``actions._UPDATE_REFUSAL_ERROR_CODES`` — so the fork's refusal keeps its
      own code instead of being flattened to ``update_not_in_place``.
    """
    original_admission = update_contract.evaluate_update_admission
    original_codes = _actions._UPDATE_REFUSAL_ERROR_CODES

    def _admission(project_root: Path):
        refusal = original_admission(project_root)
        if refusal is not None:
            return refusal
        try:
            return update_contract.evaluate_update_force_requirement(
                project_root, home=_fork_home(project_root)
            )
        except Exception:
            # The orchestrator re-runs this; fail open here so a transient read
            # error doesn't 500 the user's "Update Now" click.
            return None

    update_contract.evaluate_update_admission = _admission
    _actions._UPDATE_REFUSAL_ERROR_CODES = {**original_codes, **_FORK_REFUSAL_ERROR_CODES}
    try:
        yield
    finally:
        update_contract.evaluate_update_admission = original_admission
        _actions._UPDATE_REFUSAL_ERROR_CODES = original_codes


@contextlib.contextmanager
def _dual_store_receipts():
    """Serve the receipt GET from BOTH stores.

    The Desktop's Update button runs the DEFAULT update path, which writes
    upstream's legacy ``logs/update_receipts/`` archive; the fork's pipeline
    orchestrator writes ``<home>/update_receipts/``. Reading only one of them
    means reading nothing for half the updates the user actually ran.

    Upstream's handler looks ``_read_latest_receipt`` up as a module global at
    call time, so swapping it makes upstream's own summary builder summarise the
    very receipt the fork serves — no frozen copy of upstream's summary shape to
    drift.
    """
    from hermes_constants import get_hermes_home
    from hermes_cli.update_receipt_fork import read_latest_update_receipt_dict

    original = _actions._read_latest_receipt
    _actions._read_latest_receipt = lambda: read_latest_update_receipt_dict(get_hermes_home())
    try:
        yield
    finally:
        _actions._read_latest_receipt = original


@router.post("/api/hermes/update")
async def update_hermes():
    """Kick off ``hermes update`` in the background — upstream's handler, with the
    fork's force-requirement gate in its place."""
    with _force_requirement_gate():
        return await _actions.update_hermes()


@router.get("/api/hermes/update/check")
async def check_hermes_update(force: bool = False, profile: Optional[str] = None):
    """Report whether an update is available — upstream's handler, with the fork's
    branch comparison: a fork-local branch is measured against main, not reported
    as an error the Desktop renders as "couldn't reach the update server"."""
    with source_check_fork.fork_branch_check():
        return await _actions.check_hermes_update(force=force, profile=profile)


@router.get("/api/hermes/update/receipt")
async def get_update_receipt(profile: Optional[str] = None):
    """The full latest update receipt plus a compact ``summary`` — upstream's
    handler, reading BOTH receipt stores and scoped to the requested profile."""
    with _config_profile_scope(profile):
        with _dual_store_receipts():
            return await _actions.get_update_receipt()


@router.post("/api/hermes/update/receipt/{receipt_id}/ack")
async def acknowledge_update_receipt(receipt_id: str, profile: Optional[str] = None):
    """Mark a durable receipt as acknowledged by id.

    Mirrors the desktop's ``acknowledgeReceipt`` API call. Profile-scoped for the
    same reason as the receipt GET — the receipt lives in the target profile's
    home, and the dashboard's home may point at a different one. ``{"ok": True}``
    on success (idempotent — a second ack is a no-op) and ``{"ok": False}`` when
    the receipt id is unknown or fails the id guard.

    The acknowledgement is flipped on the receipt itself, in whichever store holds
    it (pipeline record or legacy archive file), so acknowledging one run never
    hides a different one.
    """
    with _config_profile_scope(profile):
        try:
            from hermes_constants import get_hermes_home
            from hermes_cli.update_receipt_fork import acknowledge_update_receipt as acknowledge

            ok = acknowledge(get_hermes_home(), receipt_id)
        except Exception:
            ok = False
    return {"ok": ok}


class _UpdateSettingsPayload(BaseModel):
    """The desktop's "Update behavior" toggles.

    Strict booleans: a payload of ``"true"`` or ``1`` is refused rather than
    coerced, so a malformed client can never arm the local-ahead bypass. Both
    fields are required — the settings file is written whole, so a partial body
    would silently reset the toggle it left out.
    """

    allow_local_ahead: StrictBool
    safe_mode_auto: StrictBool


@router.post("/api/hermes/update/settings")
async def save_update_settings(payload: _UpdateSettingsPayload):
    """Persist the renderer's update-behavior toggles for the orchestrator.

    The orchestrator runs as a separate spawned process and reads
    ``HERMES_HOME/update_settings.json`` on every invoke, so that file is the
    only channel that reaches it. The renderer kept the toggles in its own
    local storage instead, which left ``hermes_cli.update_settings`` with a
    writer and no callers — and the gate the toggles arm (the local-ahead
    refusal) unreachable from the UI.

    Not profile-scoped: the toggles configure the INSTALL's updater, and the
    orchestrator reads the home of the install it updates — not a profile's.
    """
    from hermes_constants import get_hermes_home
    from hermes_cli.update_settings import UpdateSettings, write_update_settings

    write_update_settings(
        get_hermes_home(),
        UpdateSettings(
            allow_local_ahead=payload.allow_local_ahead,
            safe_mode_auto=payload.safe_mode_auto,
        ),
    )
    return {"ok": True}
