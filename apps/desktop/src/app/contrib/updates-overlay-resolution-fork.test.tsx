/**
 * Which update overlay the app mounts — FORK-OWNED.
 *
 * Upstream's `app/contrib/wiring.tsx` imports `{ UpdatesOverlay }` from
 * `'../updates-overlay'`. The fork's overlay (upstream's overlay plus a
 * receipt-driven error pane that takes the dialog over after a failed update)
 * used to be wired in by EDITING that import -- a MODIFICATION of an upstream
 * line in the file upstream rewrites most often (90 commits since 2026-09-01),
 * so a routine upstream edit turned into a merge conflict and aborted
 * `hermes update` with the GUI's "Update failed (exit 1)" banner.
 *
 * The fork's overlay is now resolved through a Vite alias
 * (`apps/desktop/vite.config.ts`), so `wiring.tsx` stays byte-identical to
 * upstream's. This test pins that the alias still points at the fork's
 * overlay: if the alias entry is dropped or renamed, the fork's receipt pane
 * silently stops rendering and the last update's failure goes unreported.
 */
import { describe, expect, it } from 'vitest'

// The specifier the app's wiring uses. Resolved through the alias.
import { UpdatesOverlay as ResolvedUpdatesOverlay } from '../updates-overlay'
import { UpdatesOverlay as ForkUpdatesOverlay } from '../updates-overlay-fork'

describe("the app's update overlay", () => {
  it('resolves to the fork\'s overlay', () => {
    expect(ResolvedUpdatesOverlay).toBe(ForkUpdatesOverlay)
  })
})
