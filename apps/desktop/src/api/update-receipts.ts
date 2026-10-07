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
 *  ``<home>/update_receipts/``. Returns ``null`` when none exists yet or the
 *  latest has already been acknowledged — the overlay only needs the receipt
 *  to render a non-success outcome, so hiding acknowledged/no-op rows keeps
 *  the boot path cheap. Profile-scoped because receipt visibility is per-home
 *  (a remote registry connection owns a different state.db than the local pool). */
export function getLastReceipt(): Promise<UpdateReceipt | null> {
  return hermesApi<{ receipt: UpdateReceipt } | null>({
    ...profileScoped(),
    path: '/api/hermes/update/receipt'
  }).then(r => r?.receipt ?? null)
}

/** Mark a durable receipt as acknowledged by id. Mirrors the Python
 *  ``update_receipt.acknowledge_receipt``; the renderer flips its
 *  ``$lastReceiptAcknowledged`` atom on success so the overlay stays closed
 *  across the next boot. The id is URI-encoded so receipt ids that contain
 *  ``/`` or other path-significant characters don't escape the route. */
export function acknowledgeReceipt(receiptId: string): Promise<{ ok: boolean }> {
  return hermesApi<{ ok: boolean }>({
    ...profileScoped(),
    path: `/api/hermes/update/receipt/${encodeURIComponent(receiptId)}/ack`,
    method: 'POST',
    body: {}
  })
}