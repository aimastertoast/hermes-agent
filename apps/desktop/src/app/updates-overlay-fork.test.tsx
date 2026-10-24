import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import { en } from '@/i18n/en'
import { forkStrings } from '@/i18n/fork'
import { $lastReceipt } from '@/store/update-receipt-fork'
import { $updateApply, $updateChecking, $updateOverlayOpen, $updateOverlayTarget, $updateStatus } from '@/store/updates'
import type { UpdateReceipt } from '@/types/hermes'

import { UpdatesOverlay } from './updates-overlay-fork'

// Mock the system API so we control the receipt hydrate. The overlay's mount
// effect calls getLastReceipt() unconditionally, so the mock needs to be set
// up before render and torn down after.
const apiMock = vi.hoisted(() => ({
  acknowledgeReceipt: vi.fn(async () => ({ ok: true })),
  getLastReceipt: vi.fn(async () => null as UpdateReceipt | null)
}))

vi.mock('@/api/update-receipts', () => apiMock)

const FAILED_RECEIPT: UpdateReceipt = {
  acknowledged: false,
  error: 'merge-conflict in conflict.txt',
  outcome: 'failed',
  receipt_id: 'r-2026-09-27-001',
  rolled_back: true,
  steps: [{ detail: 'merge-conflict in conflict.txt', name: 'merge', ok: false }]
}

const SUCCESS_RECEIPT: UpdateReceipt = {
  acknowledged: false,
  outcome: 'success',
  post_state: { state_db_hash: 'def456abc123' },
  receipt_id: 'r-2026-09-27-success'
}

const UP_TO_DATE_STATUS = {
  behind: 0,
  fetchedAt: Date.now(),
  supported: true
}

afterEach((): void => {
  cleanup()
  $updateOverlayOpen.set(false)
  $updateOverlayTarget.set('client')
  $updateStatus.set(null)
  $updateChecking.set(false)
  $updateApply.set({
    applying: false,
    command: null,
    error: null,
    log: [],
    message: '',
    percent: null,
    stage: 'idle'
  })
  $lastReceipt.set(null)
  Reflect.deleteProperty(window, 'hermesDesktop')
  vi.restoreAllMocks()
  apiMock.getLastReceipt.mockReset()
  apiMock.acknowledgeReceipt.mockReset()
  apiMock.acknowledgeReceipt.mockResolvedValue({ ok: true })
  apiMock.getLastReceipt.mockResolvedValue(null)
})

async function renderOverlay(): Promise<void> {
  // The mount effect calls getLastReceipt(); wrap render in act so the
  // resulting state updates flush before assertions run.
  await act(async (): Promise<void> => {
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <UpdatesOverlay />
      </I18nProvider>
    )
  })
}

beforeEach((): void => {
  $updateOverlayOpen.set(true)
  $updateOverlayTarget.set('client')
  $updateStatus.set(null)
  $lastReceipt.set(null)
})

describe('UpdatesOverlay — receipt-driven idle view', (): void => {
  it('shows the default check-failed idle view when no receipt has loaded yet', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(null)

    await renderOverlay()

    // The receipt-driven pane is gated behind a present, non-success, not-yet-
    // acknowledged receipt. With no receipt the overlay falls through to the
    // existing generic idle copy.
    expect(screen.queryByTestId('updates-overlay-receipt-error')).toBeNull()
    expect(screen.getByText(en.updates.checkFailedTitle)).toBeTruthy()
    expect(screen.getByText(en.updates.tryAgain)).toBeTruthy()
  })

  it('does not render the receipt-driven pane when the receipt outcome is success', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(SUCCESS_RECEIPT)

    await renderOverlay()
    await waitFor(() => {
      expect($lastReceipt.get()).toEqual(SUCCESS_RECEIPT)
    })

    // success receipt: pane hidden, generic check-failed idle still shows
    expect(screen.queryByTestId('updates-overlay-receipt-error')).toBeNull()
    expect(screen.getByText(en.updates.checkFailedTitle)).toBeTruthy()
  })

  it('renders the receipt-driven error pane with the actual error line when a failed receipt is unacknowledged', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)

    await renderOverlay()
    await waitFor(() => {
      expect($lastReceipt.get()).toEqual(FAILED_RECEIPT)
    })

    // The receipt-derived line ("Update failed: merge-conflict in
    // conflict.txt") and the failed step detail should both surface — the
    // generic cantReach / checkFailedTitle lie must not appear.
    const pane = screen.getByTestId('updates-overlay-receipt-error')
    expect(pane).toBeTruthy()
    expect(pane.textContent).toContain('merge-conflict in conflict.txt')
    expect(pane.textContent).not.toContain(en.updates.cantReach)
    expect(screen.queryByText(en.updates.checkFailedTitle)).toBeNull()

    // Acknowledge / View receipt / Check now actions are all present.
    expect(screen.getByTestId('updates-overlay-acknowledge')).toBeTruthy()
    expect(screen.getByText(forkStrings.updateReceipt.viewReceipt)).toBeTruthy()
    expect(screen.getByText(en.updates.checkNow)).toBeTruthy()
  })

  it('falls back to the default idle view when the failed receipt has already been acknowledged', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue({ ...FAILED_RECEIPT, acknowledged: true })

    await renderOverlay()
    await waitFor(() => {
      expect($lastReceipt.get()?.acknowledged).toBe(true)
    })

    // Acknowledged receipt: pane hidden, generic copy returns.
    expect(screen.queryByTestId('updates-overlay-receipt-error')).toBeNull()
    expect(screen.getByText(en.updates.checkFailedTitle)).toBeTruthy()
  })

  it('keeps the receipt pane visible when a live status reports the install is up to date', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)
    // A live status only says whether an update is AVAILABLE NOW. It must not
    // hide a failed receipt — the poller fills the status in seconds, and
    // gating on "no status at all" made the pane unreachable in the app.
    $updateStatus.set(UP_TO_DATE_STATUS)

    await renderOverlay()
    await waitFor(() => {
      expect($lastReceipt.get()).toEqual(FAILED_RECEIPT)
    })

    expect(screen.getByTestId('updates-overlay-receipt-error')).toBeTruthy()
    expect(screen.getByTestId('updates-overlay-receipt-error').textContent).toContain('merge-conflict in conflict.txt')
  })

  it('keeps the receipt pane even when an update is available', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)
    $updateStatus.set({ behind: 3, fetchedAt: Date.now(), supported: true, updateAvailable: true })

    await renderOverlay()
    await waitFor(() => {
      expect($lastReceipt.get()).toEqual(FAILED_RECEIPT)
    })

    // A FAILED update leaves the checkout BEHIND origin, so `behind > 0` is
    // exactly the state the failure happens in. The pane must win there:
    // offering the same update that just failed is the wrong thing to show.
    expect(screen.getByTestId('updates-overlay-receipt-error')).toBeTruthy()
  })

  it('keeps the receipt pane visible while a check is in flight', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)
    $updateChecking.set(true)

    await renderOverlay()
    await waitFor(() => {
      expect($lastReceipt.get()).toEqual(FAILED_RECEIPT)
    })

    // A live check must not tear the pane down and rebuild it — the receipt
    // is still the truth about the last run.
    expect(screen.getByTestId('updates-overlay-receipt-error')).toBeTruthy()
  })

  it('does not render the receipt pane while an update is being applied', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)
    $updateApply.set({
      applying: true,
      command: null,
      error: null,
      log: [],
      message: '',
      percent: 10,
      stage: 'fetch'
    })

    await renderOverlay()
    await waitFor(() => {
      expect($lastReceipt.get()).toEqual(FAILED_RECEIPT)
    })

    // An apply in flight owns the view: the receipt pane is idle-phase copy
    // and would show stale "the update failed" text over a running update.
    expect(screen.queryByTestId('updates-overlay-receipt-error')).toBeNull()
  })

  it('clicking Acknowledge marks the receipt acknowledged and calls acknowledgeReceipt', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)
    apiMock.acknowledgeReceipt.mockResolvedValue({ ok: true })

    await renderOverlay()
    await waitFor(() => {
      expect(screen.getByTestId('updates-overlay-acknowledge')).toBeTruthy()
    })

    fireEvent.click(screen.getByTestId('updates-overlay-acknowledge'))

    await waitFor(() => {
      expect($lastReceipt.get()?.acknowledged).toBe(true)
    })
    expect(apiMock.acknowledgeReceipt).toHaveBeenCalledWith(FAILED_RECEIPT.receipt_id)

    // Optimistic local update: the pane closes without waiting on the round-
    // trip. The Acknowledge button is gone once the flag flips.
    expect(screen.queryByTestId('updates-overlay-acknowledge')).toBeNull()
  })

  it('brings the pane back when the server refuses the acknowledgement', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)
    // The server answers {ok: false} -- a RESOLVED promise, not a rejection.
    apiMock.acknowledgeReceipt.mockResolvedValue({ ok: false })

    await renderOverlay()
    await waitFor(() => {
      expect(screen.getByTestId('updates-overlay-acknowledge')).toBeTruthy()
    })

    fireEvent.click(screen.getByTestId('updates-overlay-acknowledge'))

    // The optimistic flag is rolled back: a refused ack must not look like a
    // successful dismissal, or the failure is swallowed for good.
    await waitFor(() => {
      expect($lastReceipt.get()?.acknowledged).toBe(false)
    })
    expect(screen.getByTestId('updates-overlay-acknowledge')).toBeTruthy()
  })

  it('retries the receipt fetch once when it fails', async (): Promise<void> => {
    vi.useFakeTimers()

    try {
      apiMock.getLastReceipt
        .mockRejectedValueOnce(new Error('backend still spawning'))
        .mockResolvedValue(FAILED_RECEIPT)

      await renderOverlay()

      expect(apiMock.getLastReceipt).toHaveBeenCalledTimes(1)

      await act(async (): Promise<void> => {
        await vi.advanceTimersByTimeAsync(2000)
      })

      // The retry is what makes the pane reachable on a slow machine: the
      // first request can land while the desktop backend is still spawning.
      expect(apiMock.getLastReceipt).toHaveBeenCalledTimes(2)
      expect($lastReceipt.get()).toEqual(FAILED_RECEIPT)
    } finally {
      vi.useRealTimers()
    }
  })

  it('fetches the receipt again when the overlay is reopened', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)

    await renderOverlay()
    await waitFor(() => {
      expect($lastReceipt.get()).toEqual(FAILED_RECEIPT)
    })

    await act(async (): Promise<void> => {
      $updateOverlayOpen.set(false)
    })
    await act(async (): Promise<void> => {
      $updateOverlayOpen.set(true)
    })

    await waitFor(() => {
      expect(apiMock.getLastReceipt).toHaveBeenCalledTimes(2)
    })
  })

  it('dispatches the auto-check while the fork owns the overlay', async (): Promise<void> => {
    const checkClientMock = vi.fn(async () => ({ fetchedAt: Date.now(), supported: true }))

    window.hermesDesktop = {
      updates: { check: checkClientMock }
    } as unknown as Window['hermesDesktop']
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)
    // The receipt is already there on the FIRST render, so the fork's dialog
    // owns the overlay from the start and upstream's overlay -- the module
    // that normally fires the auto-check -- is never mounted.
    $lastReceipt.set(FAILED_RECEIPT)

    await renderOverlay()

    await waitFor(() => {
      expect(checkClientMock).toHaveBeenCalled()
    })
    // ONE check per open: upstream's effect re-fires whenever a check ends
    // without a status, which would tear the fork's dialog down and rebuild
    // it on every pass.
    expect(checkClientMock).toHaveBeenCalledTimes(1)
    // ...and the check came from the FORK: upstream's own overlay also fires
    // exactly one check, so the count alone cannot tell them apart. The
    // fork's dialog being on screen proves upstream's overlay was never
    // mounted, so this check is the fork's.
    expect(screen.getByTestId('updates-overlay-receipt-error')).toBeTruthy()
  })
})
