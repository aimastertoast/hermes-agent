/**
 * Fork-owned additions to the desktop update store.
 *
 * `store/updates.ts` is upstream-owned and rewritten often, so the fork's
 * behaviour lives here: upstream's file only re-exports the two toggle atoms
 * (every existing consumer keeps importing them from `@/store/updates`) and
 * calls the relaunch helper at the two points where an in-place update can
 * leave the running renderer on a stale bundle.
 */

import { Codecs, persistentAtom } from '@/lib/persisted'
import { $connection } from '@/store/session'

// ── Update-settings atoms ───────────────────────────────────────────────────
//
// Renderer-side toggles for the orchestrator's user-facing modes. The
// orchestrator is the source of truth for what runs; these atoms only feed it
// user preference on each invoke. The durable update receipt is NOT here: it
// lives in the fork-owned store/update-receipt-fork.ts, because the receipt's
// acknowledged state is a per-receipt fact the server owns.

/** When true, the orchestrator's auto-classifier may run `hermes update`
 *  unattended for changes it can prove safe (no user code touched). Default
 *  off: the user must opt in once. Persisted — the orchestrator reads it on
 *  every invoke. */
export const $updateSafeModeAuto = persistentAtom<boolean>('hermes.update.safeModeAuto', false, Codecs.bool)

/** When true, force the update to proceed past the
 *  ``LOCAL_AHEAD_REFUSAL`` gate (the user's local checkout has commits that
 *  have not been pushed to the active channel's origin). The orchestrator's
 *  force-requirement gate (``evaluate_update_force_requirement``) consults
 *  this toggle on every invoke and only blocks the user on
 *  ``local-ahead`` / ``force-pushed`` states when the toggle is OFF.
 *
 *  The setting was previously named ``$updateForceModeLocal``, which
 *  described a DIFFERENT feature (force the apply to target the local
 *  checkout instead of an active remote backend) that was never wired up.
 *  Renamed to reflect the actual semantic: it arms the local-ahead bypass
 *  documented in ``update_contract.LOCAL_AHEAD_REFUSAL`` and surfaced as
 *  the "Allow Update Now when local is ahead" toggle in
 *  ``UpdatesSettings``. Default off: the user must opt in once per install.
 *  Persisted — the orchestrator reads it on every invoke.
 */
export const $updateAllowLocalAhead = persistentAtom<boolean>('hermes.update.allowLocalAhead', false, Codecs.bool)

/**
 * Ask main to re-exec Hermes.exe so a freshly rebuilt renderer bundle actually
 * loads. Returns true when the relaunch was requested; false when the bridge is
 * missing (non-desktop run) or main declined (e.g. a detached updater hand-off
 * already owns the relaunch).
 */
export function requestDesktopRelaunch(): Promise<boolean> {
  const relaunch = window.hermesDesktop?.updates?.relaunchAfterUpdate

  if (typeof relaunch !== 'function') {
    return Promise.resolve(false)
  }

  return relaunch()
    .then(result => !!result?.ok)
    .catch(() => false)
}

/**
 * After an in-place update that rebuilt the bundle, re-exec the app when the
 * running renderer is provably behind the updated tree (bundle skew). Scoped to
 * a LOCAL backend — a remote backend lives on another machine, so its update
 * must never relaunch this GUI.
 *
 * The in-app suicide-guard refuses to taskkill its own parent, so a local
 * backend update that rebuilt the desktop bundle used to leave the OLD bundle
 * serving the UI. Best-effort: never let a relaunch probe break the apply
 * result.
 */
export async function relaunchIfBundleStaleAfterInPlaceUpdate(): Promise<void> {
  if ($connection.get()?.mode === 'remote') {
    return
  }

  try {
    const version = await window.hermesDesktop?.getVersion?.()

    if (version?.bundleOutOfSync) {
      await requestDesktopRelaunch()
    }
  } catch {
    // Best-effort: never let a relaunch probe break the apply result.
  }
}
