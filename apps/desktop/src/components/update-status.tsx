import { useStore } from '@nanostores/react'
import { type ReactElement, type ReactNode, useState } from 'react'

import { BrandMark } from '@/components/brand-mark'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import type { DesktopUpdateStatus, DesktopVersionInfo } from '@/global'
import { type Translations, useI18n } from '@/i18n'
import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { shortVersion } from '@/lib/version-label'
import {
  $backendUpdateApply,
  $backendUpdateChecking,
  $backendUpdateStatus,
  $updateApply,
  $updateChecking,
  $updateStatus,
  checkBackendUpdates,
  checkUpdates,
  openUpdateOverlayFor,
  startActiveUpdate,
  type UpdateApplyState,
  type UpdateTarget
} from '@/store/updates'
import type { UpdateReceipt } from '@/types/hermes'

const RELEASE_NOTES_URL = 'https://github.com/NousResearch/hermes-agent/releases'
const INSTALLER_URL = 'https://hermes-agent.nousresearch.com/'

export type UpdateStatusTone = 'idle' | 'available' | 'error' | 'unsupported'

export interface UpdateStatusView {
  line: string
  tone: UpdateStatusTone
  error?: string
  updateAvailable: boolean
  applying: boolean
  supported: boolean
}

function retirementStatus(
  retirement: NonNullable<DesktopUpdateStatus['retirement']>,
  applying: boolean,
  supported: boolean,
  u: Translations['updates']
): UpdateStatusView {
  // Suffixed-identity build: nothing to download or migrate — just the notice.
  return {
    applying,
    supported,
    updateAvailable: false,
    tone: 'error',
    line: u.discontinuedTitle,
    error: u.discontinuedBody
  }
}

/**
 * One status derivation for every "am I up to date?" surface (About page,
 * updates overlay). Pure so the tone/copy contract is unit-testable.
 */
interface UpdateStatusInput {
  apply: UpdateApplyState
  checking: boolean
  status: DesktopUpdateStatus | null
  target: UpdateTarget
  u: Translations['updates']
}

export function deriveUpdateStatus(input: UpdateStatusInput): UpdateStatusView {
  const { apply, status, target, u } = input

  if (target === 'client' && status?.retirement) {
    return retirementStatus(
      status.retirement,
      apply.applying || apply.stage === 'restart',
      status.supported !== false,
      u
    )
  }

  return ordinaryUpdateStatus(input)
}

function ordinaryUpdateStatus({ apply, checking, status, target, u }: UpdateStatusInput): UpdateStatusView {
  const behind = status?.behind ?? 0
  // behind is null when the exact count is unknowable (shallow clone): the
  // backend flags that case via updateAvailable instead of a number.
  const updateAvailable = behind > 0 || Boolean(status?.updateAvailable)
  const supported = status?.supported !== false
  const applying = apply.applying || apply.stage === 'restart'

  if (!supported) {
    return { applying, line: status?.message ?? u.unsupportedMessage, supported, tone: 'unsupported', updateAvailable }
  }

  if (status?.error) {
    return {
      applying,
      error: [status.message, status.error].filter(l => !!l).join('\n'),
      line: u.cantReach,
      supported,
      tone: 'error',
      updateAvailable
    }
  }

  if (applying) {
    return { applying, line: u.installing, supported, tone: 'available', updateAvailable }
  }

  if (updateAvailable) {
    return {
      applying,
      line: behind > 0 ? u.updateReady(behind) : u.updateReadyUnknown,
      supported,
      tone: 'available',
      updateAvailable
    }
  }

  if (status) {
    return {
      applying,
      line: target === 'backend' ? u.latestBodyBackend : u.latestBody,
      supported,
      tone: 'idle',
      updateAvailable
    }
  }

  return { applying, line: checking ? u.checking : u.tapCheck, supported, tone: 'idle', updateAvailable }
}

/** Receipt-driven overlay status. The orchestrator writes the truth
 *  (`outcome`, `error`, step detail, `post_state`) into an `UpdateReceipt`;
 *  deriving the user's status line from that record guarantees we surface the
 *  actual failure instead of faking a generic "couldn't reach" lie.
 *
 *  The existing {@link deriveUpdateStatus} (About-page card) stays on its
 *  liveness-based path: it only knows the *check* failed, not what the
 *  orchestrator already learned. This new function is for the updates
 *  overlay, which reads the receipt. */
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
  u: Translations['updates']
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

function relativeTime(ms: number | undefined, u: Translations['updates']): string {
  if (!ms) {
    return u.never
  }

  const diff = Date.now() - ms

  if (diff < 60_000) {
    return u.justNow
  }

  if (diff < 3_600_000) {
    return u.minAgo(Math.round(diff / 60_000))
  }

  if (diff < 86_400_000) {
    return u.hoursAgo(Math.round(diff / 3_600_000))
  }

  return u.daysAgo(Math.round(diff / 86_400_000))
}

/**
 * The "Hermes Desktop / version / brand mark" hero shared by the About page
 * and the updates overlay, including the bundle-out-of-sync warning. The
 * heading render is injectable so a dialog surface can emit a DialogTitle
 * for its accessible name while About keeps a plain h2.
 */
export function VersionHero({
  renderHeading,
  version
}: {
  renderHeading?: (heading: string) => ReactNode
  version: DesktopVersionInfo | null
}): ReactElement {
  const { t } = useI18n()
  const u = t.updates

  return (
    <div className="flex flex-col items-center gap-3 pt-6 pb-2 text-center">
      <BrandMark className="size-16" />
      <div>
        {renderHeading ? (
          renderHeading(u.appName)
        ) : (
          <h2 className="text-lg font-semibold tracking-tight">{u.appName}</h2>
        )}
        <p className="mt-1 text-xs text-muted-foreground">
          {version?.appVersion ? u.version(shortVersion(version.appVersion)) : u.versionUnavailable}
          {version?.channel
            ? ` · ${Object.entries(u.channels).find(([name]: [string, string]): boolean => name === version.channel)?.[1] ?? version.channel}`
            : ''}
        </p>
      </div>
      {(version?.bundleSwapPending || version?.bundleOutOfSync) && (
        <div className="mx-auto w-full max-w-2xl rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-left text-sm">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <div className="min-w-0">
              <p className="font-medium">{version.bundleSwapPending ? u.bundleSwapPending : u.bundleOutOfSync}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {version.bundleSwapPending ? u.bundleSwapPendingDesc : u.bundleOutOfSyncDesc}
              </p>
              {version.bundleSwapPending ? (
                <Button
                  className="mt-2"
                  onClick={() => void window.hermesDesktop?.relaunchApp?.()}
                  size="sm"
                  variant="textStrong"
                >
                  <RefreshCw className="size-3" />
                  {u.bundleSwapPendingAction}
                </Button>
              ) : (
                <Button asChild className="mt-2" size="sm" variant="textStrong">
                  <a
                    href={INSTALLER_URL}
                    onClick={event => {
                      event.preventDefault()
                      void window.hermesDesktop?.openExternal?.(INSTALLER_URL)
                    }}
                    rel="noreferrer"
                    target="_blank"
                  >
                    <ExternalLink className="size-3" />
                    {u.bundleOutOfSyncAction}
                  </a>
                </Button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

interface UpdateActionsProps {
  target: UpdateTarget
  u: Translations['updates']
  view: UpdateStatusView
}

function UpdateActions({ target, u, view }: UpdateActionsProps): ReactElement | null {
  if (view.applying) {
    return null
  }

  if (!view.updateAvailable || !view.supported) {
    return null
  }

  return (
    <>
      <Button onClick={() => startActiveUpdate(target)} size="sm">
        {u.updateNow}
      </Button>
      <Button onClick={() => openUpdateOverlayFor(target)} size="sm" variant="textStrong">
        {u.seeWhatsNew}
      </Button>
    </>
  )
}

/**
 * The bordered update-state card (status line, last-checked age, check /
 * update / release-notes actions) in the About page's visual language.
 * Target-aware: 'client' reads the desktop self-update atoms, 'backend' the
 * remote backend's. The actual apply flow stays in the updates overlay —
 * "Update now" opens it and starts the install there.
 */
export function UpdateStatusCard({
  showReleaseNotes = true,
  target
}: {
  showReleaseNotes?: boolean
  target: UpdateTarget
}): ReactElement {
  const { t } = useI18n()
  const u = t.updates
  const isBackend = target === 'backend'
  const status = useStore(isBackend ? $backendUpdateStatus : $updateStatus)
  const checking = useStore(isBackend ? $backendUpdateChecking : $updateChecking)
  const apply = useStore(isBackend ? $backendUpdateApply : $updateApply)
  const [justChecked, setJustChecked] = useState<boolean>(false)

  const view = deriveUpdateStatus({ apply, checking, status, target, u })

  const handleCheck = async (): Promise<void> => {
    setJustChecked(false)
    const next = await (isBackend ? checkBackendUpdates({ force: true }) : checkUpdates({ force: true }))
    setJustChecked(Boolean(next))
  }

  return (
    <div
      className={cn(
        'rounded-xl border px-4 py-3 text-sm',
        view.tone === 'available' && 'border-primary/30 bg-primary/5 text-foreground',
        view.tone === 'error' && 'border-destructive/35 bg-destructive/5 text-destructive',
        (view.tone === 'idle' || view.tone === 'unsupported') && 'border-border/70 bg-muted/20 text-foreground'
      )}
    >
      <div className="flex items-start gap-2">
        {view.tone === 'available' ? (
          <Codicon className="mt-0.5 size-4 shrink-0 text-primary" name="cloud-download" size="1rem" />
        ) : view.tone === 'error' || view.tone === 'unsupported' ? null : (
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
        )}
        <div className="min-w-0">
          <p className="font-medium">{view.line}</p>
          {view.error && <p className="mt-1 text-xs text-muted-foreground">{view.error}</p>}
          {view.tone !== 'unsupported' && (
            <p className="mt-1 text-xs text-muted-foreground">
              {u.lastChecked(relativeTime(status?.fetchedAt, u))}
              {justChecked && !checking ? u.justNowSuffix : ''}
            </p>
          )}
        </div>
      </div>

      {view.tone !== 'unsupported' && (
        <div className="mt-3 flex flex-wrap items-center gap-4">
          <Button
            disabled={checking || view.applying}
            onClick={() => void handleCheck()}
            size="sm"
            variant="textStrong"
          >
            {checking ? <Loader2 className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
            {checking ? u.checkingShort : u.checkNow}
          </Button>

          <UpdateActions target={target} u={u} view={view} />

          {showReleaseNotes && (
            <Button asChild className="ml-auto" size="sm" variant="text">
              <a
                href={RELEASE_NOTES_URL}
                onClick={event => {
                  event.preventDefault()
                  void window.hermesDesktop?.openExternal?.(RELEASE_NOTES_URL)
                }}
                rel="noreferrer"
                target="_blank"
              >
                <ExternalLink className="size-3" />
                {u.releaseNotes}
              </a>
            </Button>
          )}
        </div>
      )}
    </div>
  )
}
