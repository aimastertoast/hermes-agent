import type { UpdateReceipt } from '@/types/hermes'

import { hermesApi, profileScoped } from './client'

/** Durable update-receipt accessors.
 *
 *  These live in their own module rather than in `./system` so that file keeps
 *  no local delta: upstream `system.ts` is edited often, and a fork-only block
 *  inside it turned every routine upstream reformat into a merge conflict that
 *  aborted `hermes update`. A brand-new file that upstream does not touch can
 *  never conflict. Mirrors the Python `hermes_cli.update_receipt` store. */

/** Fetch the most-recent durable update receipt the backend has on disk under
 *  ``<home>/update_receipts/``. Returns ``null`` when none exists yet.
 *  Profile-scoped because receipt visibility is per-home
 *  (a remote registry connection owns a different state.db than the local pool).
 *
 *  The receipt carries its OWN ``acknowledged`` flag, and the backend does NOT
 *  filter acknowledged receipts out: the renderer needs to tell "never shown"
 *  from "already dismissed", and that is a per-receipt fact the server owns.
 *  An acknowledged receipt is still returned here -- the renderer decides what
 *  to do with it. */
export function getLastReceipt(): Promise<UpdateReceipt | null> {
  return hermesApi<{ receipt: UpdateReceipt } | null>({
    ...profileScoped(),
    path: '/api/hermes/update/receipt'
  }).then(r => r?.receipt ?? null)
}

/** Mark a durable receipt as acknowledged by id. Mirrors the Python
 *  ``update_receipt.acknowledge_receipt``; the server flips the receipt's own
 *  ``acknowledged`` flag, so the pane stays closed across the next boot
 *  without the renderer keeping a second, global copy of that fact. The id is
 *  URI-encoded so receipt ids that contain ``/`` or other path-significant
 *  characters don't escape the route. */
export function acknowledgeReceipt(receiptId: string): Promise<{ ok: boolean }> {
  return hermesApi<{ ok: boolean }>({
    ...profileScoped(),
    path: `/api/hermes/update/receipt/${encodeURIComponent(receiptId)}/ack`,
    method: 'POST',
    body: {}
  })
}
