import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const storage = new Map<string, string>()

vi.mock('@/lib/storage', () => ({
  readKey: (key: string) => storage.get(key) ?? null,
  writeKey: (key: string, value: null | string) => {
    if (value === null) {
      storage.delete(key)
    } else {
      storage.set(key, value)
    }
  }
}))

// The fork module reads the connection mode at CALL time, so the mode is a
// knob the test turns rather than a fixed value.
const { atom } = await import('nanostores')
const $mockConnection = atom<{ mode: 'local' | 'remote' } | null>(null)

vi.mock('@/store/session', () => ({ $connection: $mockConnection }))

const {
  $updateAllowLocalAhead,
  $updateSafeModeAuto,
  relaunchIfBundleStaleAfterInPlaceUpdate,
  requestDesktopRelaunch
} = await import('./updates-fork')

const desktop = window as unknown as { hermesDesktop?: unknown }

function stubBridge(bridge: unknown): void {
  desktop.hermesDesktop = bridge
}

describe('updates-fork: bundle-stale relaunch', () => {
  beforeEach(() => {
    $mockConnection.set({ mode: 'local' })
    stubBridge(undefined)
  })

  afterEach(() => {
    stubBridge(undefined)
  })

  it('re-execs the app when the local bundle is provably behind', async () => {
    const relaunchAfterUpdate = vi.fn().mockResolvedValue({ ok: true })

    stubBridge({
      getVersion: vi.fn().mockResolvedValue({ bundleOutOfSync: true }),
      updates: { relaunchAfterUpdate }
    })

    await relaunchIfBundleStaleAfterInPlaceUpdate()

    expect(relaunchAfterUpdate).toHaveBeenCalledTimes(1)
  })

  it('never relaunches a remote backend (it lives on another machine)', async () => {
    $mockConnection.set({ mode: 'remote' })

    const relaunchAfterUpdate = vi.fn().mockResolvedValue({ ok: true })
    const getVersion = vi.fn().mockResolvedValue({ bundleOutOfSync: true })

    stubBridge({ getVersion, updates: { relaunchAfterUpdate } })

    await relaunchIfBundleStaleAfterInPlaceUpdate()

    expect(relaunchAfterUpdate).not.toHaveBeenCalled()
    expect(getVersion).not.toHaveBeenCalled()
  })

  it('does not relaunch when the bundle is in sync', async () => {
    const relaunchAfterUpdate = vi.fn().mockResolvedValue({ ok: true })

    stubBridge({
      getVersion: vi.fn().mockResolvedValue({ bundleOutOfSync: false }),
      updates: { relaunchAfterUpdate }
    })

    await relaunchIfBundleStaleAfterInPlaceUpdate()

    expect(relaunchAfterUpdate).not.toHaveBeenCalled()
  })

  it('is best-effort: a missing bridge or a throwing probe never breaks the apply', async () => {
    stubBridge(undefined)

    await expect(relaunchIfBundleStaleAfterInPlaceUpdate()).resolves.toBeUndefined()

    stubBridge({ getVersion: vi.fn().mockRejectedValue(new Error('probe failed')) })

    await expect(relaunchIfBundleStaleAfterInPlaceUpdate()).resolves.toBeUndefined()
  })
})

describe('updates-fork: requestDesktopRelaunch', () => {
  afterEach(() => {
    stubBridge(undefined)
  })

  it('reports whether main relaunched the app', async () => {
    stubBridge({ updates: { relaunchAfterUpdate: vi.fn().mockResolvedValue({ ok: true }) } })

    expect(await requestDesktopRelaunch()).toBe(true)

    stubBridge({ updates: { relaunchAfterUpdate: vi.fn().mockResolvedValue({ ok: false }) } })

    expect(await requestDesktopRelaunch()).toBe(false)
  })

  it('returns false when the bridge is missing or throws', async () => {
    stubBridge(undefined)

    expect(await requestDesktopRelaunch()).toBe(false)

    stubBridge({ updates: { relaunchAfterUpdate: vi.fn().mockRejectedValue(new Error('nope')) } })

    expect(await requestDesktopRelaunch()).toBe(false)
  })
})

describe('updates-fork: settings atoms', () => {
  it('default to off and persist through the storage choke point', () => {
    expect($updateSafeModeAuto.get()).toBe(false)
    expect($updateAllowLocalAhead.get()).toBe(false)

    $updateSafeModeAuto.set(true)
    $updateAllowLocalAhead.set(true)

    expect(storage.get('hermes.update.safeModeAuto')).toBe('true')
    expect(storage.get('hermes.update.allowLocalAhead')).toBe('true')
  })
})
