/**
 * Receipt-driven overlay status -- FORK-OWNED test.
 *
 * These tests cover `update-receipt-status.ts`, a fork-owned module, so they
 * live in a fork-owned file. They used to sit in upstream's
 * `update-status.test.tsx`, which put an 88-line fork delta on an upstream
 * file: upstream's own edits to that file then collided with the fork's
 * block, and the conflict aborted `hermes update` (the Desktop's "Update
 * failed (exit 1)" banner).
 */

import { describe, expect, it } from 'vitest'

import { en } from '@/i18n/en'
import { forkStrings } from '@/i18n/fork'

import { deriveUpdateStatusFromReceipt } from './update-receipt-status'

// Receipt-driven overlay status (Task 11). Derives the user's status line from
// the orchestrator's UpdateReceipt instead of from liveness — surfaces the real
// failure rather than faking u.cantReach. See plan Task 11 / spec Section 3.
describe('deriveUpdateStatusFromReceipt', () => {
  it('failed receipt: shows the actual error from the receipt, never the generic cantReach', () => {
    const receipt = {
      outcome: 'failed',
      error: 'post-state-db-modified',
      rolled_back: true,
      steps: [{ name: 'verify', detail: 'expected sha256=abc123, got def456' }]
    } as const

    const view = deriveUpdateStatusFromReceipt(receipt, forkStrings.updateReceipt)

    // The line MUST contain the actual receipt error — not the generic
    // cantReach lie the liveness-based About card used to show.
    expect(view.line).not.toBe(en.updates.cantReach)
    expect(view.line).toContain('post-state-db-modified')
    // The detail (containing the sha mismatch) must be surfaced, not hidden.
    expect(view.error).toContain('abc123')
    expect(view.error).toContain('def456')
    // A rolled-back update is recoverable — the user gets a "view receipt" CTA.
    expect(view.tone).toBe('success')
    expect(view.action).toBe('view-receipt')
  })

  it('success receipt: success tone, line mentions the post-state sha prefix', () => {
    const receipt = {
      outcome: 'success',
      post_state: { state_db_hash: 'abcdef0123456789...' },
      error: null,
      rolled_back: false,
      steps: []
    } as const

    const view = deriveUpdateStatusFromReceipt(receipt, forkStrings.updateReceipt)

    expect(view.tone).toBe('success')
    expect(view.line).toContain('abcdef0')
    expect(view.line).not.toContain(en.updates.cantReach)
    expect(view.action).toBeNull()
    expect(view.error).toBeNull()
  })

  it('conflict receipt: warning tone, --resolve action, backup branch surfaced in the line', () => {
    const receipt = {
      outcome: 'conflict',
      error: 'merge-conflict',
      rolled_back: false,
      steps: [{ name: 'merge', detail: ['config.yaml', 'agents/registry.json'] }],
      backup_ref: 'backup-123'
    } as const

    const view = deriveUpdateStatusFromReceipt(receipt, forkStrings.updateReceipt)

    expect(view.tone).toBe('warning')
    expect(view.action).toBe('resolve-conflict')
    // The backup ref is the actionable identifier for --resolve, surface it.
    expect(view.line).toContain('backup-123')
    // And the conflicted files too — that's the why for the conflict.
    expect(view.line).toContain('config.yaml')
    expect(view.line).toContain('agents/registry.json')
  })

  it('null receipt: idle empty status', () => {
    const view = deriveUpdateStatusFromReceipt(null, forkStrings.updateReceipt)

    expect(view.tone).toBe('idle')
    expect(view.line).toBe('')
    expect(view.error).toBeNull()
    expect(view.action).toBeNull()
  })

  it('failed receipt that did NOT roll back: error tone, still view-receipt action', () => {
    const receipt = {
      outcome: 'failed',
      error: 'gateway-swap-failed',
      rolled_back: false,
      steps: [{ name: 'gateway_swap', ok: false, detail: 'new-health-timeout' }]
    } as const

    const view = deriveUpdateStatusFromReceipt(receipt, forkStrings.updateReceipt)

    expect(view.tone).toBe('error')
    expect(view.action).toBe('view-receipt')
    expect(view.line).toContain('gateway-swap-failed')
    expect(view.error).toContain('new-health-timeout')
  })
})
