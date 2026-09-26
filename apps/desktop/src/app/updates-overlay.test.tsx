import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import { en } from '@/i18n/en'
import type { UpdateReceipt } from '@/types/hermes'
import {
  $lastReceipt,
  $lastReceiptAcknowledged,
  $updateOverlayOpen,
  $updateOverlayTarget,
  $updateStatus
} from '@/store/updates'

import { UpdatesOverlay } from './updates-overlay'

// Mock the system API so we control the receipt hydrate. The overlay's mount
// effect calls getLastReceipt() unconditionally, so the mock needs to be set
// up before render and torn down after.
const apiMock = vi.hoisted(() => ({
  acknowledgeReceipt: vi.fn(async () => ({ ok: true })),
  getLastReceipt: vi.fn(async () => null as UpdateReceipt | null)
}))

vi.mock('@/api/system', () => apiMock)

const FAILED_RECEIPT: UpdateReceipt = {
  error: 'merge-conflict in conflict.txt',
  outcome: 'failed',
  receipt_id: 'r-2026-09-27-001',
  rolled_back: true,
  steps: [{ detail: 'merge-conflict in conflict.txt', name: 'merge', ok: false }]
}

const SUCCESS_RECEIPT: UpdateReceipt = {
  outcome: 'success',
  post_state: { state_db_hash: 'def456abc123' },
  receipt_id: 'r-2026-09-27-success'
}

afterEach((): void => {
  cleanup()
  $updateOverlayOpen.set(false)
  $updateOverlayTarget.set('client')
  $updateStatus.set(null)
  $lastReceipt.set(null)
  $lastReceiptAcknowledged.set(true)
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
  $lastReceiptAcknowledged.set(true)
})

describe('UpdatesOverlay — receipt-driven idle view', (): void => {
  it('shows the default check-failed idle view when no receipt has loaded yet', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(null)
    $lastReceiptAcknowledged.set(true)

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
    $lastReceiptAcknowledged.set(false)

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
    $lastReceiptAcknowledged.set(false)

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
    expect(screen.getByText(en.updates.viewReceipt)).toBeTruthy()
    expect(screen.getByText(en.updates.checkNow)).toBeTruthy()
  })

  it('falls back to the default idle view when the failed receipt has already been acknowledged', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)
    $lastReceiptAcknowledged.set(true)

    await renderOverlay()
    await waitFor(() => {
      expect($lastReceipt.get()).toEqual(FAILED_RECEIPT)
    })

    // Acknowledged receipt: pane hidden, generic copy returns.
    expect(screen.queryByTestId('updates-overlay-receipt-error')).toBeNull()
    expect(screen.getByText(en.updates.checkFailedTitle)).toBeTruthy()
  })

  it('clicking Acknowledge calls acknowledgeReceipt and flips the local acknowledged flag', async (): Promise<void> => {
    apiMock.getLastReceipt.mockResolvedValue(FAILED_RECEIPT)
    apiMock.acknowledgeReceipt.mockResolvedValue({ ok: true })
    $lastReceiptAcknowledged.set(false)

    await renderOverlay()
    await waitFor(() => {
      expect(screen.getByTestId('updates-overlay-acknowledge')).toBeTruthy()
    })

    fireEvent.click(screen.getByTestId('updates-overlay-acknowledge'))

    await waitFor(() => {
      expect($lastReceiptAcknowledged.get()).toBe(true)
    })
    expect(apiMock.acknowledgeReceipt).toHaveBeenCalledWith(FAILED_RECEIPT.receipt_id)

    // Optimistic local update: the pane closes without waiting on the round-
    // trip. The Acknowledge button is gone once the flag flips.
    expect(screen.queryByTestId('updates-overlay-acknowledge')).toBeNull()
  })
})