/**
 * The fork's contribution crash guard — FORK-OWNED.
 *
 * A contribution that throws on every render used to spin the renderer: the
 * slot re-rendered it, it threw again, the boundary caught it, repeat — ~30%
 * CPU forever, with no way out but reloading the window. The guard counts the
 * failures and, past the cap, stops rendering the broken subtree at all, so
 * nothing can re-mount it.
 *
 * It lives here, in a fork-owned module, because the boundary it protects
 * (`./boundary.tsx`) is upstream's file: upstream rewrites it, so a fork
 * change there is a conflict waiting to happen — and a conflict aborts
 * `hermes update` (the GUI's "Update failed (exit 1)" banner). The guard
 * hooks into upstream's boundary through two pure insertions: one hook call
 * and one `onError` prop.
 */
import { useCallback, useState } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { ErrorState } from '@/components/ui/error-state'

/** How many failure episodes a contribution may take before its slot is locked.
 *
 *  One episode is one failed render: React re-renders the failed subtree once
 *  to recover from it, so the boundary catches it twice, but that is still a
 *  single failure and must never lock a slot. The cap is 2 — the third
 *  failure locks the slot — so an ordinary bad render keeps upstream's
 *  fallback and a retry is still offered. */
const MAX_CONTRIB_CRASHES = 2

export interface ContribCrashGuard {
  /** Failure episodes this boundary instance has seen. */
  crashCount: number
  /** True once the cap is passed: the subtree must not be rendered again. */
  isLocked: boolean
  /** Report one more failure (wired to the boundary's `onError`). */
  reportCrash: () => void
}

/** The guard's state for one contribution boundary. */
export function useContribCrashGuard(): ContribCrashGuard {
  const [crashCount, setCrashCount] = useState(0)
  const reportCrash = useCallback(() => setCrashCount(count => count + 1), [])

  return { crashCount, isLocked: crashCount > MAX_CONTRIB_CRASHES, reportCrash }
}

interface ContribSlotLockedProps {
  /** Contribution key, shown in the post-mortem. */
  id: string
  /** `chip` = inline bar item (tiny post-mortem); `pane` = zone body. */
  variant: 'chip' | 'pane'
  /** How many failures the slot took. */
  crashCount: number
}

/** The permanent post-mortem: the slot is locked, and the contribution will
 *  not be rendered again on this page. */
export function ContribSlotLocked({ crashCount, id, variant }: ContribSlotLockedProps) {
  if (variant === 'chip') {
    return (
      <span
        className="inline-flex items-center gap-1 rounded px-1.5 text-[0.6875rem] text-muted-foreground"
        title={`${id}: crashed ${crashCount} times — slot locked`}
      >
        <Codicon name="warning" size="0.7rem" />
        {id} (locked)
      </span>
    )
  }

  return (
    <div className="grid h-full place-items-center p-6">
      <ErrorState
        description={`${id} crashed ${crashCount} times in a row — slot locked to stop the renderer retry loop. Reload the window (Ctrl+R) once the underlying issue is fixed.`}
        title={`«${id}» failed to render`}
      />
    </div>
  )
}
