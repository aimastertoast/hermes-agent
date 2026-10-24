import type { ForkUpdateReceiptStrings } from '@/i18n/fork'
import type { UpdateReceipt } from '@/types/hermes'

/** Receipt-driven overlay status. The orchestrator writes the truth
 *  (`outcome`, `error`, step detail, `post_state`) into an `UpdateReceipt`;
 *  deriving the user's status line from that record guarantees we surface the
 *  actual failure instead of faking a generic "couldn't reach" lie.
 *
 *  The existing {@link deriveUpdateStatus} (About-page card) stays on its
 *  liveness-based path: it only knows the *check* failed, not what the
 *  orchestrator already learned. This new function is for the updates
 *  overlay, which reads the receipt.
 *
 *  FORK-OWNED: kept out of `components/update-status.tsx` (an upstream file)
 *  so the fork has zero delta there. The file used to be `merge=union`, and a
 *  merge where upstream inserted code at the same anchor silently dropped a
 *  closing brace, breaking the desktop build. Fork code that upstream never
 *  touches belongs in a fork-owned module like this one. */
export interface ReceiptStatusView {
  line: string
  tone: 'idle' | 'success' | 'warning' | 'error'
  error: string | null
  action: 'resolve-conflict' | 'view-receipt' | null
}

interface ReceiptLike {
  // Outcome is the only required field — partial fixtures in tests need it.
  outcome: UpdateReceipt['outcome']
  backup_ref?: string
  error?: string | null
  post_state?: Record<string, unknown>
  rolled_back?: boolean
  steps?: ReadonlyArray<{ detail?: unknown; name: string; ok?: boolean }>
}

export function deriveUpdateStatusFromReceipt(
  receipt: ReceiptLike | null,
  u: ForkUpdateReceiptStrings
): ReceiptStatusView {
  if (!receipt) {
    return { action: null, error: null, line: '', tone: 'idle' }
  }
  if (receipt.outcome === 'success') {
    const postHash = receipt.post_state?.['state_db_hash']
    const sha = typeof postHash === 'string' ? postHash.slice(0, 7) : ''
    return {
      action: null,
      error: null,
      line: u.updateSucceeded.replace('{sha}', sha),
      tone: 'success'
    }
  }
  if (receipt.outcome === 'conflict') {
    const mergeStep = receipt.steps?.find(s => s.name === 'merge')
    const detail = mergeStep?.detail
    const files =
      Array.isArray(detail) ? detail.join(', ') : typeof detail === 'string' ? detail : ''
    const backup = receipt.backup_ref ?? ''
    // Surface the backup ref in the line itself so the user sees it without
    // expanding the error — it's the actionable identifier for --resolve.
    const line = backup
      ? `${u.updateConflict.replace('{files}', files)} (branch ${backup})`
      : u.updateConflict.replace('{files}', files)
    return {
      action: 'resolve-conflict',
      error: u.updateConflictResolve.replace('{backup}', backup),
      line,
      tone: 'warning'
    }
  }
  // failed / aborted / partial / catastrophic / no-op: show the actual error
  // from the receipt, never u.cantReach. The receipt is the source of truth.
  const failedStep = receipt.steps?.find(s => s.ok === false || s.ok === undefined)
  const detail = failedStep?.detail
  const errorDetail =
    typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : ''
  // A rolled-back update means we restored pre-state — the user is safe even
  // though the receipt's outcome is non-success. Surface that honestly.
  const tone: ReceiptStatusView['tone'] = receipt.rolled_back ? 'success' : 'error'
  return {
    action: 'view-receipt',
    error: errorDetail,
    line: u.updateFailed
      .replace('{outcome}', receipt.outcome)
      .replace('{error}', receipt.error ?? 'unknown'),
    tone
  }
}
