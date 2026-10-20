"""Fork-owned additions to the update pipeline — FORK-OWNED.

``hermes_cli/update_cmd.py`` is upstream-owned and is merged on every update, so every line
the fork adds there is a merge-conflict magnet: a conflict stops the update outright (the
GUI's "Update failed (exit 1)"). The fork's own logic therefore lives HERE, in a file
upstream has never heard of, and ``update_cmd.py`` keeps only the thin call sites it cannot
avoid.

What lives here:

1. The guarded import of the Windows gateway-lifecycle surface. ``hermes update`` merges
   upstream BEFORE it syncs dependencies, so an upstream change that adds an import to
   ``hermes_cli/update_cmd_windows.py`` can leave the updater un-importable — and an updater
   that cannot import cannot update. The guard keeps the update path alive.
2. The guarded import of the self-heal drift check (fork-owned module, still guarded because
   a patch drift can strip it).
3. The transient-network retry installer, the local-only branch probe, the pre-update drift
   gate and the orchestrator CLI wrapper.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

# --------------------------------------------------------------------------------------
# 1. Guarded import of the Windows gateway-lifecycle surface
# --------------------------------------------------------------------------------------
# ``hermes_cli/update_cmd.py`` imports upstream's Windows-lifecycle symbols from
# ``hermes_cli.update_cmd_windows`` with upstream's OWN import statement, so that
# statement stays byte-identical to upstream's (a modified line is a merge-conflict
# magnet, and the file has to stay a pure insertion to be union-mergeable).
#
# The guard lives HERE instead: ``hermes update`` merges upstream BEFORE it syncs
# dependencies, so an upstream change that adds an import to
# ``hermes_cli/update_cmd_windows.py`` can leave the updater un-importable -- and an
# updater that cannot import cannot update. When the real module cannot be imported, a
# stub is published under the same name in ``sys.modules`` so upstream's import statement
# resolves against the stub. The stub answers ANY name, so a symbol upstream added or
# renamed does not break the import either.


def _stub(*_args, **_kwargs) -> None:
    return None


def _predicate(*_args, **_kwargs) -> bool:
    return False


def _list_stub(*_args, **_kwargs) -> list:
    return []


def _stub_surface_getattr(name: str):
    """PEP 562 fallback for a symbol upstream added to (or renamed in) the Windows surface.

    Dunder names raise: ``hasattr(module, "__path__")`` must stay False, or the import
    machinery would treat the stub as a package.
    """
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    return _stub


try:
    from hermes_cli import update_cmd_windows as _windows_module
except ImportError:
    _windows_module = None

if _windows_module is None:
    # The real module is unavailable — most likely upstream added an import this checkout has
    # not installed yet. Every symbol degrades to a no-op so the update can still run; the
    # Windows gateway lifecycle is skipped, everything else proceeds.
    from types import SimpleNamespace as _SimpleNamespace

    print(
        "WARNING: hermes_cli.update_cmd_windows is not importable — Windows gateway-lifecycle "
        "calls will be skipped this run. The updater needs a dependency upstream added but "
        "this checkout has not installed yet; the update continues without that lifecycle.",
        file=sys.stderr,
    )
    _WINDOWS_SURFACE_FALLBACKS: dict[str, object] = {
        "_HOLDER_VALUE_FLAGS_FALLBACK": frozenset(),
        "_holder_value_flags_cache": _SimpleNamespace(
            get=lambda *a, **kw: None, set=lambda *a, **kw: None, clear=lambda: None),
        "_cold_start_windows_gateway_after_update": _predicate,
        "_desktop_owns_gateway_lifecycle": _predicate,
        "_looks_like_desktop_control_plane": _predicate,
        "_detect_venv_python_processes": _list_stub,
        "_self_and_non_gateway_ancestor_pids": _list_stub,
        "_venv_launcher_ancestors": _list_stub,
        "_hermes_holder_subcommand": _stub,
        "_holder_value_flags": _stub,
        "_pause_windows_gateways_for_update": _stub,  # callers branch on the return value
        "_refresh_bootstrap_cache_scripts": _stub,
        "_refresh_windows_gateway_launchers": _stub,
        "_refuse_gateway_ancestor_tree_kill": _stub,
        "_restore_windows_gateway_service": _stub,
        "_resume_windows_gateways_after_update": _stub,
        "_resume_windows_gateways_and_merge_outcome": _stub,
        "_set_update_applied_new_code": _stub,
        "_start_windows_gateway_service": _stub,
        "_stop_windows_gateway_service": _stub,
        "_wait_for_windows_update_gateway_exit": _stub,
        "_write_update_planned_stop_marker": _stub,
    }
    globals().update(_WINDOWS_SURFACE_FALLBACKS)

    # Publish the stub under the real module's name so upstream's own import statement in
    # ``update_cmd.py`` resolves against it instead of raising ImportError.
    _windows_stub = types.ModuleType("hermes_cli.update_cmd_windows")
    for _name in _WINDOWS_SURFACE_FALLBACKS:
        setattr(_windows_stub, _name, globals()[_name])
    _windows_stub.__getattr__ = _stub_surface_getattr
    sys.modules["hermes_cli.update_cmd_windows"] = _windows_stub
    _parent_package = sys.modules.get("hermes_cli")
    if _parent_package is not None:
        _parent_package.update_cmd_windows = _windows_stub
else:
    # Re-export the module's public surface so ``from hermes_cli.update_cmd_fork import
    # <name>`` keeps working when upstream adds a symbol: the names are copied at import
    # time, so upstream's list can grow without the fork's import list having to follow it.
    for _name in dir(_windows_module):
        if _name.startswith("__"):
            continue
        _value = getattr(_windows_module, _name)
        if isinstance(_value, types.ModuleType):
            continue
        globals()[_name] = _value


def __getattr__(name: str):
    """Resolve a Windows-lifecycle symbol that upstream renamed or removed.

    ``update_cmd.py`` imports upstream's symbol list from this module. When upstream
    renames one of them, the merge brings the new name into ``update_cmd.py`` while the
    old name is gone from ``update_cmd_windows`` — a plain import would then raise
    ImportError and brick the updater. Return a no-op stub instead: the affected call is
    skipped, the update still runs.
    """
    if name.startswith("_"):
        globals()[name] = _stub
        return _stub
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --------------------------------------------------------------------------------------
# 2. Guarded import of the self-heal surface
# --------------------------------------------------------------------------------------
try:
    from hermes_cli.update_cmd_selfheal import preflight_drift_check, restore_patches
except ImportError:  # a missing restorer must not brick the CLI update paths
    class _SelfHealNoopResult:
        """Duck-type of a successful no-op restore result (fallback)."""

        patches_applied = ()
        patches_drifted = ()
        patches_with_marker_fail = ()
        fatal_error = None
        bundle_rebuild_required = False

        def __getattr__(self, name):
            return ()

    def restore_patches(project_root=None, **_kwargs):
        print(
            "WARNING: hermes_cli.update_cmd_selfheal not importable — patch self-heal skipped "
            "this run. Restore from Workspace/agents/hermes-patches/selfheal/ (the post-merge "
            "hook does this automatically).",
            file=sys.stderr,
        )
        return _SelfHealNoopResult()

    class _PreflightNoopReport:
        """Duck-type for the pre-update drift check (fallback).

        Reports empty (no drift) so a missing selfheal module does not block the update
        path. Drift is still caught at restore time.
        """

        drifted = ()
        skipped_no_meta = ()
        skipped_no_upstream = ()
        fatal_error = None

        def __getattr__(self, name):
            return ()

        @property
        def is_blocking(self):
            return False

    def preflight_drift_check(project_root=None, **_kwargs):
        print(
            "WARNING: hermes_cli.update_cmd_selfheal not importable — pre-update drift check "
            "skipped this run. Drift will be caught at restore time instead (slower, post-merge).",
            file=sys.stderr,
        )
        return _PreflightNoopReport()


# --------------------------------------------------------------------------------------
# 3. The fork's own update-path helpers
# --------------------------------------------------------------------------------------
def remote_has_branch(root: Path, branch: str) -> bool:
    """True iff ``origin`` actually has ``branch``.

    A fork install sits on a branch that exists on no remote, and the desktop
    hands that branch to the updater as ``--branch`` (it reads it from the
    install stamp). Fetching it dies with "couldn't find remote ref", so ask
    the remote before trusting the flag. ``ls-remote --exit-code`` exits 2 when
    no ref matches; any other non-zero (offline, auth) is also "not proven" —
    the caller's fallback is the same either way.
    """
    from hermes_cli._subprocess_compat import no_prompt_git_kwargs

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-remote", "--exit-code", "--heads", "origin", branch],
            capture_output=True, text=True, check=False,
            timeout=30, **no_prompt_git_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def resolve_local_only_branch(args, branch: str, *, project_root) -> str:
    """Re-pin a local-only ``--branch`` to the channel's own branch; return the branch to use.

    A fork install's branch is local-only, and the desktop passes it as ``--branch``;
    fetching it fails with "couldn't find remote ref". What the user wants is the channel's
    own branch merged INTO the local one. Re-pin the subscription to that branch instead of
    dropping it: ``args.branch = None`` would let the channel's release pin land DETACHED at
    its exact commit, abandoning the maintained branch. With ``origin/<branch>`` as the
    target the parked-branch guard stays in charge and merges in place under
    ``updates.parked_branch_strategy=update_in_place``.
    """
    from hermes_cli.update_cmd import _source_update_channel

    if not getattr(args, "branch", None):
        return branch
    if remote_has_branch(project_root, branch):
        return branch
    selected_channel = _source_update_channel(args)
    print(f"  (branch {branch} is local-only; updating from the {selected_channel} channel instead)")
    args.branch = branch = "main"
    return branch


def check_pre_update_drift(*, project_root, upstream_ref: str) -> None:
    """Refuse the update when an upstream rewrite would strip a local patch.

    Runs AFTER the fetch (so ``origin/main`` is fresh) but BEFORE the merge at
    ``_prepare_checkout_for_update`` (so abort-on-drift is mutation-free). For each active
    patch, materialize upstream's version of the target file into a temp mirror, then
    ``git apply --check`` against it. Any failure => upstream rewrote the file in a way the
    local patch can no longer accommodate; abort with a per-patch report so the patch can be
    regenerated against the new upstream before the user re-runs update.

    Nothing is mutated on abort. Returns normally when the update may proceed.
    """
    from hermes_cli.update_cmd import _finalize_receipt

    preflight = preflight_drift_check(project_root=project_root, upstream_ref=upstream_ref)
    if preflight.drifted:
        print()
        print("  X PRE-UPDATE DRIFT DETECTED — update aborted, nothing mutated:")
        print("    The upstream rewrite of one or more files would strip a local patch.")
        print("    Regenerate the patch(es) against the new upstream, then re-run update.")
        print()
        for name, relpath, reason in preflight.drifted:
            print(f"      DRIFT  {name}")
            print(f"             file: {relpath}")
            print(f"             reason: {reason}")
            print()
        for name in preflight.skipped_no_meta:
            print(f"      SKIP   {name} (no target_relpath in .meta.json)")
        if preflight.skipped_no_upstream:
            for name, relpath in preflight.skipped_no_upstream:
                print(f"      SKIP   {name} (target {relpath} not on {upstream_ref} — local addition)")
        _finalize_receipt(
            "partial",
            f"Pre-update drift check refused: {len(preflight.drifted)} patch(es) drifted against {upstream_ref}")
        sys.exit(1)
    if preflight.fatal_error:
        print(f"  X PRE-UPDATE DRIFT CHECK FATAL: {preflight.fatal_error} — aborting (cannot guarantee safe merge).")
        _finalize_receipt("partial", f"Pre-update drift check fatal: {preflight.fatal_error}")
        sys.exit(1)
    if preflight.skipped_no_meta or preflight.skipped_no_upstream:
        print(f"  (preflight: {len(preflight.skipped_no_meta)} patch(es) skipped (no meta), "
              f"{len(preflight.skipped_no_upstream)} skipped (local-only target))")


def install_transient_retry(namespace: dict) -> None:
    """Wrap the module's ``_git_run`` so a transient network failure is retried.

    Installed from the END of ``update_cmd.py`` instead of by rewriting ``_git_run``'s body:
    that function is upstream-owned, and rewriting it was the single largest merge conflict
    the fork carried on this file. The wrapper defers the ``check=True`` raise until the
    retries are spent, so a transient failure stays a ``CompletedProcess`` the retry layer
    can classify instead of an exception it would have to unpack.
    """
    from hermes_cli.update_transient_retry import with_transient_retry

    base = namespace["_git_run"]

    def _git_run(git_cmd, args, cwd=None, *, check=False, network=False):
        def once():
            return base(git_cmd, args, cwd=cwd, check=False, network=network)

        label = f"git {args[0] if args else ''}"
        result = with_transient_retry(once, label=label) if network else once()
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr)
        return result

    namespace["_git_run"] = _git_run


def orchestrator_outcome_to_exit_code(outcome) -> int:
    """Translate an ``UpdateOutcome`` into a CLI exit code.

    The orchestrator's ``exit_code`` field defaults to ``0`` for every terminal
    branch that does not explicitly override it (success, aborted, conflict,
    failed, partial, catastrophic, no-op). Only the ``receipt-write-failed``
    success branch sets it to ``1``. We rely on ``outcome.exit_code`` when it
    is set, and otherwise fall back to a deterministic mapping so callers see
    a non-zero exit for any non-success outcome — matches the legacy path's
    contract and the test cases in
    ``hermes_cli/tests/update/test_update_cmd_wrapper.py``.
    """
    if outcome.exit_code:
        return outcome.exit_code
    if outcome.outcome in ("success", "no-op"):
        return 0
    return 1


def cmd_update_via_orchestrator(args, gateway_mode: bool) -> int:
    """Run the new ``run_update`` orchestrator as the update entry point.

    Thin wrapper around ``hermes_cli.update_orchestrator.run_update``. Wired
    in ``cmd_update`` behind the ``--use-orchestrator`` flag so the legacy
    ``_cmd_update_impl`` path stays the default. Returns the CLI exit code
    directly so the click handler can ``sys.exit(code)`` it.

    ``gateway_mode`` is accepted for parity with ``_cmd_update_impl`` and is
    currently unused: the orchestrator handles gateway pause/resume itself
    via the ``gateway`` kwarg, which is not yet exposed on the CLI surface
    (no gateway currently runs while the orchestrator is the entry point).
    """
    from hermes_cli.config import get_hermes_home
    from hermes_cli.update_cmd import _m
    from hermes_cli.update_orchestrator import run_update as _run_update

    repo = _m().PROJECT_ROOT
    home = Path(get_hermes_home())

    print("☤ Updating Hermes Agent (orchestrator)...")

    outcome = _run_update(
        repo=repo,
        home=home,
        force=getattr(args, "force", False),
        auto_apply_safe=getattr(args, "auto_apply_safe", False),
    )

    if outcome.outcome == "conflict":
        # Conflicts are left in MERGE_HEAD by the orchestrator (per spec). The
        # user resolves them out-of-band and re-runs update; we surface the
        # receipt id and the conflict markers so the operator knows the work
        # that is sitting on disk.
        print(f"✗ Update aborted: merge conflicts (receipt {outcome.receipt_id or '<none>'})")
        for step in outcome.steps:
            if step.get("name") == "merge" and not step.get("ok", True):
                detail = step.get("detail") or []
                if isinstance(detail, (list, tuple)):
                    for path in detail:
                        print(f"    CONFLICT  {path}")
                elif detail:
                    print(f"    CONFLICT  {detail}")
        print(f"    Resolve with: cd {repo} && git merge --abort   # or resolve manually, then re-run")
    elif outcome.outcome == "aborted":
        print(f"✗ Update aborted: {outcome.error or 'unknown'}")
    elif outcome.outcome == "failed":
        print(f"✗ Update failed: {outcome.error or 'unknown'}"
              + (" (rolled back from snapshot)" if outcome.rolled_back else ""))
    elif outcome.outcome == "partial":
        print(f"✗ Update partial: {outcome.error or 'gateway-swap-failed'}")
    elif outcome.outcome == "catastrophic":
        print(f"✗ Update catastrophic: {outcome.error or 'unknown'} (snapshot restore failed)")
    elif outcome.outcome == "no-op":
        print(f"→ Update skipped: {outcome.error or 'not a git install'}")
    # success: orchestrator already wrote a receipt; nothing else to print.

    return orchestrator_outcome_to_exit_code(outcome)
