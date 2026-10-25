import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $updateAllowLocalAhead } from '@/store/updates-fork'

import { UpdatesSettings } from './updates-settings'

// vi.mock is hoisted to the top of the file, so the mock-internal atoms have
// to live inside a vi.hoisted factory that runs at hoist time. At that point
// nanostores is not yet importable as a module, so the test uses a tiny atom
// shim with the same `.get() / .set() / .listen()` surface `@nanostores/react`
// needs to wire `useStore`. `useStore` calls `store.listen(callback)` and the
// callback fires synchronously once with the current value, then on every set.
interface MockAtom<T> {
  get(): T
  set(value: T): void
  listen(listener: (value: T) => void): () => void
  value: T
}

function createMockAtom<T>(initial: T): MockAtom<T> {
  let current = initial
  const listeners = new Set<(value: T) => void>()

  const atom: MockAtom<T> = {
    get: () => current,
    listen: listener => {
      listeners.add(listener)
      listener(current)

      return () => {
        listeners.delete(listener)
      }
    },
    set: value => {
      if (current === value) {
        return
      }

      current = value
      atom.value = value

      for (const listener of listeners) {
        listener(value)
      }
    },
    value: initial
  }

  return atom
}

const atoms = vi.hoisted(() => ({
  $mockAllowLocalAhead: createMockAtom(false)
}))

const mocks = vi.hoisted(() => ({
  notifyError: vi.fn(),
  saveUpdateSettings: vi.fn((_toggles: { allowLocalAhead: boolean }) => Promise.resolve({ ok: true }))
}))

vi.mock('@/api/update-settings', () => ({
  saveUpdateSettings: (toggles: { allowLocalAhead: boolean }) => mocks.saveUpdateSettings(toggles)
}))

vi.mock('@/store/notifications', () => ({
  notifyError: (...args: unknown[]) => mocks.notifyError(...args)
}))

vi.mock('@/store/updates-fork', async (): Promise<Record<string, unknown>> => {
  const actual = await import('@/store/updates-fork')

  return {
    ...actual,
    $updateAllowLocalAhead: atoms.$mockAllowLocalAhead
  }
})

describe('UpdatesSettings', (): void => {
  afterEach((): void => {
    cleanup()
    atoms.$mockAllowLocalAhead.set(false)
    mocks.saveUpdateSettings.mockClear()
    mocks.notifyError.mockClear()
  })

  it('renders the update-mode toggle with the persisted label', (): void => {
    render(<UpdatesSettings />)

    expect(screen.getByRole('heading', { name: 'Update behavior' })).toBeTruthy()
    expect(screen.getByRole('switch', { name: 'Allow Update Now when local is ahead' })).toBeTruthy()
    expect($updateAllowLocalAhead.get()).toBe(false)
  })

  it('does not offer the auto-apply safe-updates switch', (): void => {
    // The switch was removed: nothing ever consumed the persisted
    // `safe_mode_auto` flag, so flipping it only ever looked like a broken
    // updater. Its absence is the contract now.
    render(<UpdatesSettings />)

    expect(screen.queryByRole('switch', { name: 'Auto-apply safe updates' })).toBeNull()
    expect(screen.queryByText(/auto-apply/i)).toBeNull()
  })

  it('reflects the atom value so a non-default initial state renders checked', (): void => {
    atoms.$mockAllowLocalAhead.set(true)
    render(<UpdatesSettings />)

    const allowAhead = screen.getByRole('switch', { name: 'Allow Update Now when local is ahead' })

    expect(allowAhead.getAttribute('aria-checked')).toBe('true')
  })

  it('clicking the allow-local-ahead toggle flips the atom value', (): void => {
    render(<UpdatesSettings />)

    const allowAhead = screen.getByRole('switch', { name: 'Allow Update Now when local is ahead' })
    expect(allowAhead.getAttribute('aria-checked')).toBe('false')

    fireEvent.click(allowAhead)

    expect($updateAllowLocalAhead.get()).toBe(true)
    expect(allowAhead.getAttribute('aria-checked')).toBe('true')

    fireEvent.click(allowAhead)

    expect($updateAllowLocalAhead.get()).toBe(false)
    expect(allowAhead.getAttribute('aria-checked')).toBe('false')
  })

  it('exposes an aria-labelledby that points at the visible heading', (): void => {
    render(<UpdatesSettings />)

    const heading = screen.getByRole('heading', { name: 'Update behavior' })
    const section = heading.closest('section[aria-labelledby]')

    expect(section).toBeTruthy()
    expect(section?.getAttribute('aria-labelledby')).toBe(heading.id)
    expect(heading.id).toBe('updates-settings-heading')
  })

  it('writes the toggle to the backend when it is flipped', (): void => {
    render(<UpdatesSettings />)

    fireEvent.click(screen.getByRole('switch', { name: 'Allow Update Now when local is ahead' }))

    expect(mocks.saveUpdateSettings).toHaveBeenCalledWith({
      allowLocalAhead: true
    })
  })

  it('surfaces a failed write instead of leaving the toggle silently unsaved', async (): Promise<void> => {
    mocks.saveUpdateSettings.mockImplementationOnce(() => Promise.reject(new Error('backend down')))
    render(<UpdatesSettings />)

    fireEvent.click(screen.getByRole('switch', { name: 'Allow Update Now when local is ahead' }))

    await Promise.resolve()
    await Promise.resolve()

    expect(mocks.notifyError).toHaveBeenCalledWith(expect.any(Error), expect.any(String))
  })
})
