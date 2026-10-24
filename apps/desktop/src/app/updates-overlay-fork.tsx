/**
 * The fork's update overlay: upstream's overlay plus a receipt-driven error pane.
 *
 * Upstream's ``updates-overlay.tsx`` is a file upstream keeps editing, so the
 * fork's lines in it were a recurring merge-conflict magnet: ``hermes update``
 * merges ``origin/main`` into the fork's branch and ABORTS on any conflict
 * (exit 1 -- the Desktop's "Update failed (exit 1)" banner). The fork's
 * behavior lives here instead:
 *
 *  * the durable update receipt is hydrated every time the overlay opens, and
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
import { deriveUpdateStatusFromReceipt } from '@/components/update-receipt-status'
import { useI18n } from '@/i18n'
import { forkStrings } from '@/i18n/fork'
import { $lastReceipt } from '@/store/update-receipt-fork'
import {
  $backendUpdateApply,
  $backendUpdateChecking,
  $backendUpdateStatus,
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

/** How long to wait before the second (and last) attempt at hydrating the
 *  receipt. Long enough for the desktop backend to finish spawning. */
const RECEIPT_HYDRATE_RETRY_MS = 2000

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

  // Hydrate the durable update receipt when the overlay OPENS -- not once per
  // mount. The overlay stays mounted for the app's whole life, so a
  // mount-time fetch that failed (backend still spawning, transport error)
  // left the pane dead for the entire session, with no way to recover except
  // a restart.
  useEffect(() => {
    if (!open) {
      return
    }

    let cancelled = false
    let retry: ReturnType<typeof setTimeout> | undefined

    const load = (): Promise<void> =>
      Promise.resolve()
        .then(() => getLastReceipt())
        .then(receipt => {
          if (!cancelled) {
            $lastReceipt.set(receipt)
          }
        })

    // One retry: the first request can land while the desktop backend is
    // still spawning, and the desktop's own spawn deadline rejects it.
    load().catch(() => {
      if (cancelled) {
        return
      }

      retry = setTimeout(() => {
        void load().catch(() => undefined)
      }, RECEIPT_HYDRATE_RETRY_MS)
    })

    return () => {
      cancelled = true

      if (retry !== undefined) {
        clearTimeout(retry)
      }
    }
  }, [open])

  const receipt = useStore($lastReceipt)

  // The pane replaces upstream's IDLE view, so it is gated on the same
  // conditions upstream gates that view on: the idle phase, and (for the
  // client) no retirement notice.
  const idlePhase = apply.stage === 'idle' && !apply.applying

  // The pane wins over upstream's "an update is available" pitch ON PURPOSE.
  // A failed update leaves the checkout BEHIND origin -- so `behind > 0` and
  // `updateAvailable` is true -- which is exactly when the user most needs to
  // read WHY the last update failed instead of being offered the same update
  // that just failed. The acknowledgement is the dismissal: once the user has
  // seen the failure, upstream's pitch comes back.
  const receiptPane =
    receipt && receipt.outcome !== 'success' && !receipt.acknowledged && idlePhase && (isBackend || !status?.retirement)
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
 * the last update run left a non-success, unacknowledged receipt.
 * Surfaces the receipt's actual outcome + step detail
 * (via `deriveUpdateStatusFromReceipt`) instead of the generic check-failed
 * copy, and offers the user a one-click Acknowledge so the pane stays closed
 * across the next boot.
 */
function ReceiptErrorView({ onRetryCheck, receipt }: { onRetryCheck: () => void; receipt: UpdateReceipt }) {
  const { t } = useI18n()
  const u = t.updates
  const f = forkStrings.updateReceipt
  const receiptView = deriveUpdateStatusFromReceipt(receipt, f)

  const handleAcknowledge = () => {
    // Optimistic local update: mark the receipt acknowledged right away so
    // the pane closes without waiting for the backend ack round-trip. The
    // server owns the durable flag (the receipt's own `acknowledged` field),
    // so a failed ack means the receipt comes back -- the correct recovery
    // behavior, as long as the failure is actually noticed: the server
    // answers `{ok: false}` (a RESOLVED promise) when it refuses the id, so
    // a `.catch` alone would treat a refusal as a success.
    $lastReceipt.set({ ...receipt, acknowledged: true })
    void acknowledgeReceipt(receipt.receipt_id)
      .then(result => {
        if (result?.ok === false) {
          $lastReceipt.set(receipt)
        }
      })
      .catch(() => {
        $lastReceipt.set(receipt)
      })
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
          {f.acknowledge}
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
            {f.viewReceipt}
          </a>
        </Button>
      </div>
    </div>
  )
}
