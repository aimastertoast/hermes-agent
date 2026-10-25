/**
 * The fork's UI strings — FORK-OWNED.
 *
 * These strings used to live as insertions inside upstream's per-locale
 * catalogs (`i18n/en.ts`, `i18n/types.ts`, `i18n/fr.ts`, `i18n/es.ts`,
 * `i18n/de.ts`) and in `i18n/overlay-gaps.json`. Upstream rewrites those files
 * in (almost) every commit — the catalogs grow a key per feature — so the
 * fork's lines there were the most likely place for a merge conflict, and a
 * conflict aborts `hermes update` with the GUI's "Update failed (exit 1)"
 * banner. They live here now, and the catalogs are byte-identical to
 * upstream's again.
 *
 * One table serves every locale: the fork's fr/es/de entries were English
 * copies carrying a "pending translator review" note, so this reproduces
 * exactly what shipped. Add a locale table when a translator picks them up.
 */

export interface ForkUpdateModeStrings {
  title: string
  allowLocalAheadLabel: string
  allowLocalAheadDescription: string
  saveFailed: string
}

export interface ForkUpdateReceiptStrings {
  updateSucceeded: string
  updateConflict: string
  updateConflictResolve: string
  updateFailed: string
  acknowledge: string
  viewReceipt: string
}

export interface ForkStrings {
  /** Renderer-side toggles for the orchestrator's update modes. */
  updateModes: ForkUpdateModeStrings
  /** Receipt-driven update status and overlay actions. */
  updateReceipt: ForkUpdateReceiptStrings
}

export const forkStrings: ForkStrings = {
  updateModes: {
    title: 'Update behavior',
    allowLocalAheadLabel: 'Allow Update Now when local is ahead',
    allowLocalAheadDescription:
      'By default, Update Now is disabled when your checkout has commits not yet in upstream. Enable this to proceed (a backup branch is created automatically).',
    saveFailed: 'Could not save the update settings — the updater will not see this change.'
  },
  updateReceipt: {
    // The placeholders match the orchestrator receipt shape, so the overlay
    // shows the actual outcome instead of a generic "couldn't reach the
    // update server" lie.
    updateSucceeded: 'Update applied successfully (state.db @ {sha}).',
    updateConflict: 'Update conflict in: {files}',
    updateConflictResolve:
      'Your local commits are on branch {backup}. Run `hermes update --resolve` to finish.',
    updateFailed: 'Update {outcome}: {error}',
    // The Acknowledge button flips the receipt's own `acknowledged` flag and
    // POSTs `/receipt/:id/ack`; View receipt deep-links into the
    // orchestrator's logged receipt.
    acknowledge: 'Acknowledge',
    viewReceipt: 'View receipt'
  }
}
