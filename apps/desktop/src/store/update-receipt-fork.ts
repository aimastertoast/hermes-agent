/**
 * The fork's durable update-receipt state.
 *
 * Fork-owned on purpose: the receipt atoms are the fork's, and upstream's
 * `store/updates.ts` is a file upstream keeps editing -- every fork line in
 * it is a hunk a future merge can trip over, and a collision aborts
 * `hermes update` with exit 1 ("Update failed (exit 1)" in the Desktop).
 * A brand-new file upstream does not touch can never conflict.
 *
 * The receipt's ACKNOWLEDGED state is per receipt and lives on the server
 * (the receipt's own `acknowledged` field, flipped by
 * `POST /api/hermes/update/receipt/{id}/ack`). The renderer used to keep a
 * global boolean here instead; nothing ever set it back to false, so it hid
 * every receipt forever and the receipt-driven pane could never render.
 */
import { atom } from 'nanostores'

import type { UpdateReceipt } from '@/types/hermes'

/** The most recent durable update receipt the renderer has loaded. Null when
 *  none exists yet.
 *
 *  The receipt's acknowledged state travels WITH the receipt (`receipt.
 *  acknowledged`), not as a separate global flag, so acknowledging one receipt
 *  never hides a different one.
 *
 *  NOT persisted: the authoritative copy lives on disk under
 *  `<home>/update_receipts/`, and the overlay hydrates this atom from the
 *  backend on mount. */
export const $lastReceipt = atom<UpdateReceipt | null>(null)
