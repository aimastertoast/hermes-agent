/**
 * The fork's update overlay: upstream's overlay plus a receipt-driven error pane.
 *
 * Upstream's ``updates-overlay.tsx`` is a file upstream keeps editing, so the
 * fork's lines in it were a recurring merge-conflict magnet: ``hermes update``
 * merges ``origin/main`` into the fork's branch and ABORTS on any conflict
 * (exit 1 -- the Desktop's "Update failed (exit 1)" banner). The fork's
 * behavior lives here instead:
 *
 *  * the durable update receipt is hydrated once per overlay mount, and
 *  * a non-success, unacknowledged receipt replaces the generic
 *    "couldn't check for updates" copy with the receipt's real outcome, an
 *    Acknowledge action and a link to the receipt.
 *
 * Everything else stays upstream's: this module renders upstream's own
 * ``UpdatesOverlay`` and only takes the dialog over while the receipt pane
 * applies, so upstream's overlay keeps improving without a re-sync.
 */
import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { acknowledgeReceipt, getLastReceipt } from '@/api/update-receipts'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  preventCloseButtonAutoFocus
} from '@/components/ui/dialog'
import { ErrorIcon } from '@/components/ui/error-state'
import { deriveUpdateStatusFromReceipt } from '@/components/update-status'
import { useI18n } from '@/i18n'
import {
  $backendUpdateApply,
  $backendUpdateChecking,
  $backendUpdateStatus,
  $lastReceipt,
  $lastReceiptAcknowledged,
  $updateApply,
  $updateChecking,
  $updateOverlayOpen,
  $updateOverlayTarget,
  $updateStatus,
  checkBackendUpdates,
  checkUpdates,
  setUpdateOverlayOpen
} from '@/store/updates'
import type { UpdateReceipt } from '@/types/hermes'

import { UpdatesOverlay as UpstreamUpdatesOverlay } from './updates-overlay'

export function UpdatesOverlay() {
  const open = useStore($updateOverlayOpen)
  const target = useStore($updateOverlayTarget)

  const clientStatus = useStore($updateStatus)
  const clientChecking = useStore($updateChecking)
  const clientApply = useStore($updateApply)
  const backendStatus = useStore($backendUpdateStatus)
  const backendChecking = useStore($backendUpdateChecking)
  const backendApply = useStore($backendUpdateApply)

  const isBackend = target === 'backend'
  const status = isBackend ? backendStatus : clientStatus
  const checking = isBackend ? backendChecking : clientChecking
  const apply = isBackend ? backendApply : clientApply
  const check = isBackend ? checkBackendUpdates : checkUpdates

  // One auto-check per open, tracked across the overlay's whole life.
  const checkedForOpen = useRef(false)

  // Hydrate the durable update receipt once per overlay mount. The receipt is
  // the source of truth for what the orchestrator actually did; without this
  // fetch the overlay would fall back to a generic "couldn't reach the update
  // server" lie when the check itself was fine but the apply failed.
  useEffect(() => {
    let cancelled = false

    // Defer the call through `Promise.resolve().then(...)` so a synchronous
    // throw from `hermesApi` (e.g. when the bridge is unavailable in tests)
    // becomes an async rejection the `.catch` below can swallow -- otherwise
    // a missing `window.hermesDesktop.api` would bubble out of the effect and
    // unmount the dialog during render.
    Promise.resolve()
      .then(() => getLastReceipt())
      .then(receipt => {
        if (cancelled) {
          return
        }

        $lastReceipt.set(receipt)
      })
      .catch(() => {
        // Best-effort: a missing receipt is normal (fresh install, network
        // down, bridge unavailable), so we don't surface it -- the overlay's
        // idle view already covers the "no receipt" state.
      })

    return () => {
      cancelled = true
    }
  }, [])

  const receipt = useStore($lastReceipt)
  const acknowledged = useStore($lastReceiptAcknowledged)

  // The pane replaces upstream's IDLE view, so it is gated on the same
  // conditions upstream gates that view on: the idle phase, and (for the
  // client) no retirement notice. It also only applies when the idle view
  // would have nothing else to render -- no live check in flight and no live
  // status -- because once a fresh check resolves to "update available" (or
  // to nothing), the receipt-driven pane no longer adds information.
  const idlePhase = apply.stage === 'idle' && !apply.applying

  const receiptPane =
    receipt &&
    receipt.outcome !== 'success' &&
    !acknowledged &&
    !checking &&
    idlePhase &&
    (isBackend || !status?.retirement) &&
    !status
      ? receipt
      : null

  // While the fork's dialog owns the overlay, upstream's own auto-check effect
  // is not mounted -- so the check that "open the overlay" owes the user has
  // to be dispatched here. ONE check per open: upstream's effect re-fires
  // every time a check ends without a status (the ``checking`` atom flips
  // back), and the fork's dialog would then be torn down and rebuilt on every
  // pass. When the fork hands the overlay back to upstream, upstream's effect
  // owns the check again -- so this one only fires while the fork's dialog is
  // the one on screen.
  // eslint-disable-next-line no-restricted-syntax -- per-open flag, not an atom mirror
  useEffect(() => {
    if (!open) {
      checkedForOpen.current = false

      return
    }

    if (!receiptPane || checkedForOpen.current || status || checking) {
      return
    }

    checkedForOpen.current = true
    void check()
  }, [check, checking, open, receiptPane, status])

  if (!receiptPane) {
    return <UpstreamUpdatesOverlay />
  }

  return (
    <Dialog onOpenChange={setUpdateOverlayOpen} open={open}>
      {/* Same shell upstream's overlay uses, so the pane takes the dialog's
          place instead of stacking a second modal on top of it. */}
      <DialogContent
        bodyClassName="overflow-hidden p-0 gap-0"
        className="max-w-sm"
        onOpenAutoFocus={preventCloseButtonAutoFocus}
      >
        <ReceiptErrorView onRetryCheck={() => void check()} receipt={receiptPane} />
      </DialogContent>
    </Dialog>
  )
}

/**
 * Receipt-driven error pane. Rendered instead of upstream's idle view when
 * the orchestrator left a non-success, unacknowledged receipt and there is no
 * live check in flight or live status. Surfaces the receipt's actual outcome +
 * step detail (via `deriveUpdateStatusFromReceipt`) instead of the generic
 * check-failed copy, and offers the user a one-click Acknowledge so the pane
 * stays closed across the next boot.
 */
function ReceiptErrorView({ onRetryCheck, receipt }: { onRetryCheck: () => void; receipt: UpdateReceipt }) {
  const { t } = useI18n()
  const u = t.updates
  const receiptView = deriveUpdateStatusFromReceipt(receipt, u)

  const handleAcknowledge = () => {
    // Optimistic local update: flip the acknowledged flag immediately so
    // the overlay closes the receipt-driven pane without waiting on the
    // backend ack round-trip. The API call is fire-and-forget; if the
    // request fails the receipt stays in the atom and a later mount
    // surfaces it again, which is the correct recovery behavior.
    $lastReceiptAcknowledged.set(true)
    void acknowledgeReceipt(receipt.receipt_id).catch(() => undefined)
  }

  return (
    <div className="grid gap-4 px-6 pb-6 pt-1 pr-8" data-testid="updates-overlay-receipt-error">
      <div className="flex flex-col items-center gap-2 text-center">
        <ErrorIcon />

        <DialogTitle className="text-center text-lg font-semibold tracking-tight">{u.errorTitle}</DialogTitle>
        <DialogDescription className="max-w-prose text-center text-sm leading-5 text-foreground">
          {receiptView.line}
        </DialogDescription>
        {receiptView.error && (
          <pre className="mt-2 max-h-32 w-full overflow-auto whitespace-pre-wrap break-words rounded-md border border-border/70 bg-muted/30 px-3 py-2 text-left text-xs text-muted-foreground">
            {receiptView.error}
          </pre>
        )}
      </div>

      <div className="flex flex-wrap items-center justify-center gap-2">
        <Button onClick={onRetryCheck} size="sm" variant="textStrong">
          {u.checkNow}
        </Button>
        <Button data-testid="updates-overlay-acknowledge" onClick={handleAcknowledge} size="sm">
          {u.acknowledge}
        </Button>
        <Button asChild size="sm" variant="text">
          <a
            href={`hermes://update/receipt/${encodeURIComponent(receipt.receipt_id)}`}
            onClick={event => {
              event.preventDefault()
              void window.hermesDesktop?.openExternal?.(
                `hermes://update/receipt/${encodeURIComponent(receipt.receipt_id)}`
              )
            }}
          >
            {u.viewReceipt}
          </a>
        </Button>
      </div>
    </div>
  )
}
