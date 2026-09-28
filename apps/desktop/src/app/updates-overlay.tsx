import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { acknowledgeReceipt, getLastReceipt } from '@/api/system'
import { BrandMark } from '@/components/brand-mark'
import { SyncStatusCard } from '@/components/sync-status-card'
import { Button } from '@/components/ui/button'
import { CopyButton, writeClipboardText } from '@/components/ui/copy-button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  preventCloseButtonAutoFocus
} from '@/components/ui/dialog'
import { ErrorIcon, ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { Progress } from '@/components/ui/progress'
import { UpdateStatusCard, VersionHero, deriveUpdateStatusFromReceipt } from '@/components/update-status'
import { VersionDetails } from '@/components/version-details'
import type {
  DesktopUpdateCommit,
  DesktopUpdateStage,
  DesktopUpdateStatus,
  DesktopVersionInfo,
  UpdaterMechanismClient
} from '@/global'
import { useI18n } from '@/i18n'
import { AlertCircle, Check, Copy, Terminal } from '@/lib/icons'
import { buildCommitChangelog, type CommitGroup, formatFullChangelogText } from '@/lib/commit-changelog'
import { resolveUpdateCopy, type UpdateTarget } from '@/lib/update-copy'
import { cn } from '@/lib/utils'
import {
  $backendUpdateApply,
  $backendUpdateChecking,
  $backendUpdateStatus,
  $desktopVersion,
  $lastReceipt,
  $lastReceiptAcknowledged,
  $updateApply,
  $updateChecking,
  $updateOverlayOpen,
  $updateOverlayTarget,
  $updateStatus,
  applyBackendUpdate,
  applyUpdates,
  checkBackendUpdates,
  checkUpdates,
  dismissDiscontinuedNotice,
  resetUpdateApplyState,
  setUpdateOverlayOpen,
  type UpdateApplyState
} from '@/store/updates'

import { DiscontinuedNotice } from './retirement-view'

function totalItems(groups: readonly CommitGroup[]) {
  return groups.reduce((sum, g) => sum + g.items.length, 0)
}

export function UpdatesOverlay() {
  const open = useStore($updateOverlayOpen)
  const target = useStore($updateOverlayTarget)

  const clientStatus = useStore($updateStatus)
  const clientChecking = useStore($updateChecking)
  const clientApply = useStore($updateApply)
  const backendStatus = useStore($backendUpdateStatus)
  const backendChecking = useStore($backendUpdateChecking)
  const backendApply = useStore($backendUpdateApply)
  const desktopVersion = useStore($desktopVersion)

  const isBackend = target === 'backend'
  const status = isBackend ? backendStatus : clientStatus
  const checking = isBackend ? backendChecking : clientChecking
  const apply = isBackend ? backendApply : clientApply
  const check = isBackend ? checkBackendUpdates : checkUpdates
  const install = isBackend ? applyBackendUpdate : applyUpdates

  useEffect(() => {
    if (open && !status && !checking) {
      void check()
    }
  }, [check, checking, open, status])

  // Hydrate the durable update receipt once per overlay mount. The receipt is
  // the source of truth for what the orchestrator actually did; without this
  // fetch the overlay would fall back to a generic "couldn't reach the update
  // server" lie when the check itself was fine but the apply failed.
  useEffect(() => {
    let cancelled = false

    // Defer the call through `Promise.resolve().then(...)` so a synchronous
    // throw from `hermesApi` (e.g. when the bridge is unavailable in tests)
    // becomes an async rejection the `.catch` below can swallow — otherwise
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
        // down, bridge unavailable), so we don't surface it — the overlay's
        // idle view already covers the "no receipt" state.
      })

    return () => {
      cancelled = true
    }
  }, [])

  const behind = status?.behind ?? 0
  const updateAvailable = status?.updateAvailable || behind > 0

  const phase: 'idle' | 'applying' | 'manual' | 'guiSkew' | 'error' =
    apply.stage === 'manual'
      ? 'manual'
      : apply.stage === 'guiSkew'
        ? 'guiSkew'
        : apply.applying || apply.stage === 'restart'
          ? 'applying'
          : apply.stage === 'error'
            ? 'error'
            : 'idle'

  const handleClose = (next: boolean) => {
    if (phase === 'applying') {
      return
    }

    setUpdateOverlayOpen(next)

    if (
      !next &&
      (apply.stage === 'error' || apply.stage === 'restart' || apply.stage === 'manual' || apply.stage === 'guiSkew')
    ) {
      resetUpdateApplyState()
    }
  }

  const handleInstall = () => {
    void install()
  }

  return (
    <Dialog onOpenChange={handleClose} open={open}>
      {/* This dialog has no inputs, so Radix's default autofocus would land on
          the close button and trigger its tooltip immediately on open. */}
      <DialogContent
        bodyClassName="overflow-hidden p-0 gap-0"
        className="max-w-sm"
        onOpenAutoFocus={preventCloseButtonAutoFocus}
        showCloseButton={phase !== 'applying'}
      >
        {phase === 'applying' && (
          <ApplyingView apply={apply} isBackend={isBackend} statusMechanism={status?.mechanism} />
        )}

        {phase === 'manual' && (
          <ManualView
            command={apply.command ?? null}
            isBackend={isBackend}
            message={apply.message}
            onDone={() => handleClose(false)}
          />
        )}

        {phase === 'guiSkew' && <GuiSkewView message={apply.message} onDone={() => handleClose(false)} />}

        {phase === 'error' ? (
          <ErrorView message={apply.message} onDismiss={() => handleClose(false)} onRetry={handleInstall} />
        ) : null}

        {phase === 'idle' && !isBackend && status?.retirement?.state === 'discontinued' && (
          <DiscontinuedNotice
            onDismiss={() => {
              dismissDiscontinuedNotice(status.retirement!)
              handleClose(false)
            }}
            retirement={status.retirement}
          />
        )}
        {phase === 'idle' && (isBackend || !status?.retirement) && (
          <IdleView
            behind={behind}
            checking={checking}
            commits={status?.commits ?? []}
            onInstall={handleInstall}
            onLater={() => handleClose(false)}
            onRetryCheck={() => void check()}
            status={status}
            target={target}
            updateAvailable={updateAvailable}
            version={desktopVersion}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function IdleView({
  behind,
  checking,
  commits,
  onInstall,
  onLater,
  onRetryCheck,
  status,
  target,
  updateAvailable,
  version
}: {
  behind: number
  checking: boolean
  commits: readonly DesktopUpdateCommit[]
  onInstall: () => void
  onLater: () => void
  onRetryCheck: () => void
  status: DesktopUpdateStatus | null
  target: UpdateTarget
  updateAvailable: boolean
  version: DesktopVersionInfo | null
}) {
  const { t } = useI18n()
  const u = t.updates
  const receipt = useStore($lastReceipt)
  const acknowledged = useStore($lastReceiptAcknowledged)
  const receiptView = deriveUpdateStatusFromReceipt(receipt, u)

  // The orchestrator ran and left a non-success outcome the user hasn't
  // dismissed yet: surface the receipt's actual error instead of the
  // generic check-failed copy. We only override the top of the view when
  // no live check is in flight and there's nothing the idle view would
  // otherwise render — once a fresh check resolves to "update available"
  // (or to nothing), the receipt-driven pane no longer adds information.
  if (receipt && receipt.outcome !== 'success' && !acknowledged && !checking && !status) {
    return <ReceiptErrorView receiptView={receiptView} receipt={receipt} onRetryCheck={onRetryCheck} u={u} />
  }

  if (!status && checking) {
    return (
      <CenteredStatus
        icon={<Loader className="size-12" label={u.checking} type="lemniscate-bloom" />}
        title={u.checking}
      />
    )
  }

  if (!status) {
    return (
      <CenteredStatus
        action={
          <Button onClick={onRetryCheck} size="sm">
            {u.tryAgain}
          </Button>
        }
        icon={<ErrorIcon />}
        title={u.checkFailedTitle}
      />
    )
  }

  const details = version ? <VersionDetails version={version} /> : null

  // App-installer check-unknown (OS checker unavailable): NOT "no updates"
  // and NOT a generic error — the OS also installs updates automatically on
  // restart (it re-reads the .appinstaller feed on every launch), so the
  // honest view names that path. Only this mechanism gets it.
  if (status.mechanism === 'app-installer' && status.error && !status.updateAvailable) {
    return (
      <div className="grid gap-4 px-6 pb-6 pt-1 pr-8">
        <VersionHero
          renderHeading={heading => (
            <DialogTitle className="text-lg font-semibold tracking-tight">{heading}</DialogTitle>
          )}
          version={version}
        />
        <div className="flex flex-col items-center gap-2 text-center">
          <AlertCircle className="size-6 text-muted-foreground" />
          <p className="text-sm font-medium">{u.checkUnknownTitleAppInstaller}</p>
          <p className="max-w-prose text-sm leading-5 text-muted-foreground">{u.checkUnknownBodyAppInstaller}</p>
        </div>
        <Button onClick={onRetryCheck} size="sm">
          {u.tryAgain}
        </Button>
        {details}
      </div>
    )
  }

  // Everything that is NOT the install pitch — unsupported, check error, and
  // already-latest — is exactly the About page's state: render the shared
  // hero + status card so the two surfaces cannot drift. The card owns the
  // check/retry actions (its "Check now" covers the old Try-again button).
  if (!status.supported || status.error || !updateAvailable) {
    return (
      <div className="grid gap-4 px-6 pb-6 pt-1 pr-8">
        <VersionHero
          renderHeading={heading => (
            <DialogTitle className="text-lg font-semibold tracking-tight">{heading}</DialogTitle>
          )}
          version={version}
        />
        <UpdateStatusCard target={target} />
        <SyncStatusCard />
        {details}
      </div>
    )
  }

  const groups = buildCommitChangelog(commits, {
    labels: {
      new: u.changeLogNew,
      fixed: u.changeLogFixed,
      faster: u.changeLogFaster,
      improved: u.changeLogImproved,
      other: u.changeLogOther
    },
    fallback: { label: u.changeLogFallbackLabel, item: u.changeLogFallbackItem }
  })

  const shownItems = totalItems(groups)
  const remaining = Math.max(0, behind - shownItems)

  // Name what's being updated. In remote mode the overlay acts on the connected
  // backend, not the local client — say so. When there are no commit rows to
  // show (e.g. pip/non-git backend), degrade to honest "no release notes" copy
  // instead of generic filler. On a release-feed channel (stable), name the
  // release tag instead of commit vocabulary.
  const { title, body } = resolveUpdateCopy({
    target,
    shownItems,
    channel: status?.channel === 'stable' ? 'stable' : 'main',
    latestTag: status?.latestTag ?? null,
    mechanism: status?.mechanism,
    copy: u
  })

  const handleCopyFullLog = () => formatFullChangelogText(commits, behind, status.branch)

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <BrandMark className="size-16" />

        <DialogTitle className="text-center text-xl">{title}</DialogTitle>
        <DialogDescription className="text-center text-sm">{body}</DialogDescription>
      </div>

      <div className="grid gap-3">
        {groups.map((group, index) => (
          <div key={group.id}>
            <div className="flex items-center justify-between gap-2">
              <p className="text-[0.625rem] font-semibold text-muted-foreground">{group.label}</p>
              {index === 0 && commits.length > 0 && (
                <CopyButton
                  appearance="icon"
                  buttonSize="icon-xs"
                  className="-my-1 size-5 shrink-0 text-muted-foreground/70 hover:text-foreground"
                  iconClassName="size-3"
                  label={u.copyFullLog}
                  side="left"
                  text={handleCopyFullLog}
                />
              )}
            </div>
            <ul className="mt-1.5 grid gap-1.5 text-xs text-foreground">
              {group.items.map(item => (
                <li className="flex items-start gap-2" key={item}>
                  <span aria-hidden className="mt-1.5 inline-block size-1 shrink-0 rounded-full bg-primary" />
                  <span className="leading-snug">{item}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <div className="grid gap-2">
        <Button className="font-semibold" onClick={onInstall} size="lg">
          {u.updateNow}
        </Button>
        <Button className="font-medium" onClick={onLater} type="button" variant="text">
          {u.maybeLater}
        </Button>
      </div>

      {remaining > 0 && <p className="text-center text-xs text-muted-foreground">{u.moreChanges(remaining)}</p>}

      <SyncStatusCard />
    </div>
  )
}

function ManualView({
  command,
  isBackend,
  message,
  onDone
}: {
  command: string | null
  isBackend: boolean
  message?: string
  onDone: () => void
}) {
  const { t } = useI18n()
  const u = t.updates
  const [copied, setCopied] = useState(false)
  const guidance: string | undefined = message && message !== command ? message : undefined

  const handleCopy = () => {
    if (!command) {
      return
    }

    void writeClipboardText(command).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1800)
    })
  }

  // No command (e.g. the Linux sandbox-blocked relaunch): render the explanatory
  // message + a Done button, not a copy-a-command box.
  if (!command) {
    return (
      <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
        <div className="flex flex-col items-center gap-3 text-center">
          <Terminal className="size-8 text-primary" />

          <DialogTitle className="text-center text-xl">
            {isBackend ? u.manualUnavailableTitle : u.manualTitle}
          </DialogTitle>
          <DialogDescription className="text-center text-sm">{message || u.manualPickedUp}</DialogDescription>
        </div>

        <Button className="font-semibold" onClick={onDone} size="lg" variant="secondary">
          {u.done}
        </Button>
      </div>
    )
  }

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <Terminal className="size-8 text-primary" />

        <DialogTitle className="text-center text-xl">{u.manualTitle}</DialogTitle>
        <DialogDescription className="text-center text-sm">
          {guidance ?? (isBackend ? u.manualBodyBackend : u.manualBody)}
        </DialogDescription>
      </div>

      <button
        className={cn(
          'group flex w-full items-center justify-between gap-3 rounded-md border px-4 py-3 text-left transition-colors',
          copied ? 'border-primary/50' : 'border-(--stroke-nous) hover:border-(--ui-stroke-secondary)'
        )}
        onClick={handleCopy}
        type="button"
      >
        <code className="min-w-0 flex-1 truncate select-all font-mono text-sm text-foreground">
          <span className="select-none text-muted-foreground">$ </span>
          {command}
        </code>
        <span
          className={cn(
            'flex shrink-0 items-center gap-1 text-xs font-medium transition-colors',
            copied ? 'text-primary' : 'text-muted-foreground group-hover:text-foreground'
          )}
        >
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          {copied ? u.copied : u.copy}
        </span>
      </button>

      {!guidance && (
        <p className="text-center text-xs text-muted-foreground">
          {isBackend ? u.manualPickedUpBackend : u.manualPickedUp}
        </p>
      )}

      <Button className="font-semibold" onClick={onDone} size="lg" variant="secondary">
        {u.done}
      </Button>
    </div>
  )
}

// Linux GUI/backend skew (#45205): backend updated, but the running desktop app
// package (AppImage/.deb/.rpm) was NOT changed. Closeable terminal state that
// tells the user to update/reinstall the desktop app — never claims the GUI was
// updated.
function GuiSkewView({ message, onDone }: { message?: string; onDone: () => void }) {
  const { t } = useI18n()
  const u = t.updates

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <AlertCircle className="size-8 text-amber-500" />

        <DialogTitle className="text-center text-xl">{u.guiSkewTitle}</DialogTitle>
        <DialogDescription className="max-w-prose text-center text-sm leading-5 text-muted-foreground">
          {message || u.guiSkewBody}
        </DialogDescription>
      </div>

      <Button className="font-semibold" onClick={onDone} size="lg" variant="secondary">
        {u.done}
      </Button>
    </div>
  )
}

function ApplyingView({
  apply,
  isBackend,
  statusMechanism
}: {
  apply: UpdateApplyState
  isBackend: boolean
  statusMechanism?: UpdaterMechanismClient
}) {
  const { t } = useI18n()
  const u = t.updates
  const label = u.stages[apply.stage as DesktopUpdateStage] ?? u.stages.idle
  const isWindowsPackage = statusMechanism === 'app-installer' || statusMechanism === 'microsoft-store'

  const body = isWindowsPackage ? u.applyingBodyAppInstaller : isBackend ? u.applyingBodyBackend : u.applyingBody

  const currentMessage = apply.message.trim()
  const recentLog = apply.log.slice(-4)

  const percent =
    typeof apply.percent === 'number' && Number.isFinite(apply.percent)
      ? Math.max(2, Math.min(100, Math.round(apply.percent)))
      : null

  return (
    <div className="grid gap-5 px-6 pb-6 pt-7">
      <div className="flex flex-col items-center gap-3 text-center">
        <Loader className="size-16" label={label} type="lemniscate-bloom" />

        <DialogTitle className="text-center text-xl">{label}</DialogTitle>
        <DialogDescription className="text-center text-sm">{body}</DialogDescription>

        {currentMessage ? (
          <p className="max-w-lg break-words text-center text-xs leading-5 text-muted-foreground">{currentMessage}</p>
        ) : null}
      </div>

      <Progress
        aria-label={label}
        indeterminate={percent === null}
        size="lg"
        value={percent === null ? 0 : percent / 100}
      />

      {recentLog.length > 1 ? (
        <div className="max-h-24 overflow-hidden rounded-md border border-border/70 bg-muted/35 px-3 py-2 text-left font-mono text-[11px] leading-4 text-muted-foreground">
          {recentLog.map((entry, index) => (
            <div className="truncate" key={`${entry.at}-${index}`}>
              {entry.message}
            </div>
          ))}
        </div>
      ) : null}

      <p className="text-center text-xs text-muted-foreground">{u.applyingClose}</p>
    </div>
  )
}

function ErrorView({ message, onDismiss, onRetry }: { message: string; onDismiss: () => void; onRetry: () => void }) {
  const { t } = useI18n()
  const u = t.updates

  return (
    <ErrorState
      className="px-6 pb-6 pt-7 pr-8"
      description={
        <DialogDescription className="max-w-prose text-center text-sm leading-5 text-muted-foreground">
          {message || u.errorBody}
        </DialogDescription>
      }
      title={<DialogTitle className="text-center text-xl font-semibold tracking-tight">{u.errorTitle}</DialogTitle>}
    >
      <Button className="font-semibold" onClick={onRetry} size="lg">
        {u.tryAgain}
      </Button>
      <Button onClick={onDismiss} variant="text">
        {u.notNow}
      </Button>
    </ErrorState>
  )
}

function CenteredStatus({
  action,
  body,
  icon,
  title
}: {
  action?: React.ReactNode
  body?: string
  icon: React.ReactNode
  title: string
}) {
  return (
    <div className="grid gap-4 px-6 pb-6 pt-8 pr-8">
      <div className="flex flex-col items-center gap-3 text-center">
        {icon}

        <DialogTitle className="text-center text-lg">{title}</DialogTitle>
        {body && <DialogDescription className="text-center text-sm">{body}</DialogDescription>}
      </div>

      {action && <div className="flex justify-center">{action}</div>}
    </div>
  )
}

/**
 * Receipt-driven error pane. Rendered from {@link IdleView} when the
 * orchestrator left a non-success, unacknowledged receipt and there is no
 * live check in flight or live status. Surfaces the receipt's actual
 * outcome + step detail (via `deriveUpdateStatusFromReceipt`) instead of
 * the generic check-failed copy, and offers the user a one-click
 * Acknowledge so the pane stays closed across the next boot.
 *
 * `useI18n()` is read inside this component (not in the caller) so the
 * receipt line and error detail resolve against the same locale as the
 * rest of the overlay.
 */
function ReceiptErrorView({
  receiptView,
  receipt,
  onRetryCheck,
  u
}: {
  receiptView: { action: string | null; error: string | null; line: string; tone: string }
  receipt: { receipt_id: string }
  onRetryCheck: () => void
  u: ReturnType<typeof useI18n>['t']['updates']
}) {
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
