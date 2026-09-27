import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DesktopUpdateStatus, DesktopVersionInfo } from '@/global'
import { I18nProvider, type Locale, TRANSLATIONS, type Translations } from '@/i18n'
import { en } from '@/i18n/en'
import type { UpdateApplyState } from '@/store/updates'

import { deriveUpdateStatus, deriveUpdateStatusFromReceipt, VersionHero } from './update-status'

// VersionHero is the shared About/overlay hero. Its module imports the real
// updates store graph; mock it shallowly — these tests exercise the hero's
// own rendering, not the store.
vi.mock('@/store/updates', async () => {
  const { atom } = await import('nanostores')

  return {
    $backendUpdateApply: atom<UpdateApplyState | null>(null),
    $backendUpdateChecking: atom<boolean>(false),
    $backendUpdateStatus: atom<DesktopUpdateStatus | null>(null),
    $updateApply: atom<UpdateApplyState | null>(null),
    $updateChecking: atom<boolean>(false),
    $updateStatus: atom<DesktopUpdateStatus | null>(null),
    checkBackendUpdates: vi.fn(),
    checkUpdates: vi.fn(),
    openUpdateOverlayFor: vi.fn(),
    startActiveUpdate: vi.fn()
  }
})

const IDLE_APPLY: UpdateApplyState = {
  applying: false,
  stage: 'idle',
  message: '',
  percent: null,
  error: null,
  command: null,
  log: []
}

function derive(status: DesktopUpdateStatus | null, apply: UpdateApplyState = IDLE_APPLY, checking = false) {
  return deriveUpdateStatus({ apply, checking, status, target: 'client', u: en.updates })
}

describe('deriveUpdateStatus', () => {
  it('never-checked: idle tone, invites a check', () => {
    const view = derive(null)

    expect(view.tone).toBe('idle')
    expect(view.updateAvailable).toBe(false)
    expect(view.line).toBe(en.updates.tapCheck)
  })

  it('unsupported wins over everything else and keeps the backend message', () => {
    const view = derive({ supported: false, message: 'managed install', behind: 5, updateAvailable: true })

    expect(view.tone).toBe('unsupported')
    expect(view.line).toBe('managed install')
    expect(view.supported).toBe(false)
  })

  it('check error shows one plain line and keeps the transport message with the error', () => {
    const view = derive({ supported: true, error: 'check-failed', message: 'ECONNREFUSED' })

    expect(view.tone).toBe('error')
    expect(view.line).toBe(en.updates.cantReach)
    expect(view.error).toBe('ECONNREFUSED\ncheck-failed')
  })

  it.each([undefined, ''])('keeps the error when the transport message is %s', message => {
    const view = derive({ supported: true, error: 'check-failed', message })

    expect(view.error).toBe('check-failed')
  })

  it('applying beats available so the card cannot offer a second install', () => {
    const view = derive({ supported: true, behind: 3 }, { ...IDLE_APPLY, applying: true, stage: 'update' })

    expect(view.applying).toBe(true)
    expect(view.tone).toBe('available')
    expect(view.line).toBe(en.updates.installing)
  })

  it('restart stage counts as applying even when the applying flag already dropped', () => {
    const view = derive({ supported: true }, { ...IDLE_APPLY, applying: false, stage: 'restart' })

    expect(view.applying).toBe(true)
  })

  it('behind count renders counted copy; count-free updateAvailable falls back', () => {
    expect(derive({ supported: true, behind: 4 }).line).toBe(en.updates.updateReady(4))

    // Shallow clone: exact count unknowable, flagged via updateAvailable.
    const unknown = derive({ supported: true, behind: 0, updateAvailable: true })

    expect(unknown.line).toBe(en.updates.updateReadyUnknown)
    expect(unknown.updateAvailable).toBe(true)
  })

  it('up to date: idle tone with the latest-version line', () => {
    const view = derive({ supported: true, behind: 0 })

    expect(view.tone).toBe('idle')
    expect(view.updateAvailable).toBe(false)
    expect(view.line).toBe(en.updates.latestBody)
  })

  it('backend target says the backend is current, not "you"', () => {
    const view = deriveUpdateStatus({
      apply: IDLE_APPLY,
      checking: false,
      status: { supported: true, behind: 0 },
      target: 'backend',
      u: en.updates
    })

    expect(view.line).toBe(en.updates.latestBodyBackend)
  })
})

describe('VersionHero bundle banners', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  const version = (over: Partial<DesktopVersionInfo>): DesktopVersionInfo =>
    ({ appVersion: '0.19.0', ...over }) as DesktopVersionInfo

  const stubRelaunch = () => {
    const relaunchApp = vi.fn().mockResolvedValue(undefined)
    const original = window.hermesDesktop
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { ...original, openExternal: vi.fn(), relaunchApp }
    })

    return relaunchApp
  }

  it('shows an R2 channel name that is not a built-in translated label', (): void => {
    render(<VersionHero version={version({ channel: 'pm-preview' })} />)

    expect(screen.getByText(`${en.updates.version('0.19.0')} · pm-preview`)).toBeTruthy()
  })

  it.each(Object.entries(TRANSLATIONS) as [Locale, Translations][])(
    'localizes version channels in %s',
    (locale: Locale, copy: Translations): void => {
      for (const channel of ['stable', 'canary'] as const) {
        const label: string = copy.updates.channels[channel]

        expect(label).toBeTypeOf('string')

        const { unmount }: { unmount: () => void } = render(
          <I18nProvider configClient={null} initialLocale={locale}>
            <VersionHero version={version({ channel })} />
          </I18nProvider>
        )

        expect(screen.getByText(`${copy.updates.version('0.19.0')} · ${label}`)).toBeTruthy()
        unmount()
      }
    }
  )

  // FAIL-BEFORE (C19): the shared About extraction dropped the swap-pending
  // restart affordance even though main still produces bundleSwapPending and
  // exposes the relaunchApp IPC — users could no longer finish an applied
  // update from the UI.
  it('swap-pending: restart banner wired to relaunchApp, not the installer link', () => {
    const relaunchApp = stubRelaunch()

    render(<VersionHero version={version({ bundleSwapPending: true })} />)

    expect(screen.getByText(en.updates.bundleSwapPending)).toBeTruthy()
    expect(screen.queryByText(en.updates.bundleOutOfSync)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: en.updates.bundleSwapPendingAction }))
    expect(relaunchApp).toHaveBeenCalledTimes(1)
  })

  it('out-of-sync without a pending swap: keeps the get-installer banner', () => {
    stubRelaunch()

    render(<VersionHero version={version({ bundleOutOfSync: true })} />)

    expect(screen.getByText(en.updates.bundleOutOfSync)).toBeTruthy()
    expect(screen.queryByText(en.updates.bundleSwapPending)).toBeNull()
    expect(screen.queryByRole('button', { name: en.updates.bundleSwapPendingAction })).toBeNull()
  })

  it('a pending swap wins over the out-of-sync banner — restart, not reinstall', () => {
    stubRelaunch()

    render(<VersionHero version={version({ bundleOutOfSync: true, bundleSwapPending: true })} />)

    expect(screen.getByText(en.updates.bundleSwapPending)).toBeTruthy()
    expect(screen.queryByText(en.updates.bundleOutOfSync)).toBeNull()
  })

  it('no flags: no banner at all', () => {
    render(<VersionHero version={version({})} />)

    expect(screen.queryByText(en.updates.bundleOutOfSync)).toBeNull()
    expect(screen.queryByText(en.updates.bundleSwapPending)).toBeNull()
  })
})

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
    const view = deriveUpdateStatusFromReceipt(receipt, en.updates)

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
    const view = deriveUpdateStatusFromReceipt(receipt, en.updates)

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
    const view = deriveUpdateStatusFromReceipt(receipt, en.updates)

    expect(view.tone).toBe('warning')
    expect(view.action).toBe('resolve-conflict')
    // The backup ref is the actionable identifier for --resolve, surface it.
    expect(view.line).toContain('backup-123')
    // And the conflicted files too — that's the why for the conflict.
    expect(view.line).toContain('config.yaml')
    expect(view.line).toContain('agents/registry.json')
  })

  it('null receipt: idle empty status', () => {
    const view = deriveUpdateStatusFromReceipt(null, en.updates)

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
    const view = deriveUpdateStatusFromReceipt(receipt, en.updates)

    expect(view.tone).toBe('error')
    expect(view.action).toBe('view-receipt')
    expect(view.line).toContain('gateway-swap-failed')
    expect(view.error).toContain('new-health-timeout')
  })
})
