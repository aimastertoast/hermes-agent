import { describe, expect, it } from 'vitest'

import { $lastReceipt } from './update-receipt-fork'

describe('$lastReceipt', (): void => {
  it('defaults to null', (): void => {
    expect($lastReceipt.get()).toBeNull()
  })

  it('holds the hydrated receipt until it is replaced', (): void => {
    const receipt = {
      error: 'merge-conflict in conflict.txt',
      outcome: 'failed',
      receipt_id: 'r-2026-09-27-001',
      rolled_back: true
    }

    $lastReceipt.set(receipt)

    expect($lastReceipt.get()).toEqual(receipt)

    $lastReceipt.set(null)

    expect($lastReceipt.get()).toBeNull()
  })

  it('carries the acknowledged flag with the receipt instead of keeping a global flag', (): void => {
    // The renderer used to keep ONE global "acknowledged" boolean next to the
    // receipt. Nothing ever set it back to false, so it hid every receipt for
    // the rest of the session. The flag now travels WITH the receipt, so
    // acknowledging one run cannot hide a different one.
    $lastReceipt.set({
      acknowledged: true,
      outcome: 'failed',
      receipt_id: 'r-2026-09-27-001'
    })
    $lastReceipt.set({
      acknowledged: false,
      outcome: 'failed',
      receipt_id: 'r-2026-09-28-002'
    })

    expect($lastReceipt.get()?.acknowledged).toBe(false)

    $lastReceipt.set(null)
  })
})
