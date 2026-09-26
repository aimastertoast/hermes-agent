import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $updateForceModeLocal, $updateSafeModeAuto } from '@/store/updates'

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
  $mockSafeMode: createMockAtom(false),
  $mockForceLocal: createMockAtom(false)
}))

vi.mock('@/store/updates', async (): Promise<Record<string, unknown>> => {
  const actual = await import('@/store/updates')

  return {
    ...actual,
    $updateSafeModeAuto: atoms.$mockSafeMode,
    $updateForceModeLocal: atoms.$mockForceLocal
  }
})

describe('UpdatesSettings', (): void => {
  afterEach((): void => {
    cleanup()
    atoms.$mockSafeMode.set(false)
    atoms.$mockForceLocal.set(false)
  })

  it('renders both update-mode toggles with the persisted labels', (): void => {
    render(<UpdatesSettings />)

    expect(screen.getByRole('heading', { name: 'Update behavior' })).toBeTruthy()
    expect(screen.getByRole('switch', { name: 'Auto-apply safe updates' })).toBeTruthy()
    expect(screen.getByRole('switch', { name: 'Allow Update Now when local is ahead' })).toBeTruthy()
    expect($updateSafeModeAuto.get()).toBe(false)
    expect($updateForceModeLocal.get()).toBe(false)
  })

  it('reflects the atom value so a non-default initial state renders checked', (): void => {
    atoms.$mockSafeMode.set(true)
    atoms.$mockForceLocal.set(false)
    render(<UpdatesSettings />)

    const safe = screen.getByRole('switch', { name: 'Auto-apply safe updates' })
    const force = screen.getByRole('switch', { name: 'Allow Update Now when local is ahead' })

    expect(safe.getAttribute('aria-checked')).toBe('true')
    expect(force.getAttribute('aria-checked')).toBe('false')
  })

  it('clicking the safe-mode toggle flips the atom value', (): void => {
    render(<UpdatesSettings />)

    const safe = screen.getByRole('switch', { name: 'Auto-apply safe updates' })
    expect(safe.getAttribute('aria-checked')).toBe('false')

    fireEvent.click(safe)

    expect($updateSafeModeAuto.get()).toBe(true)
    expect(safe.getAttribute('aria-checked')).toBe('true')

    fireEvent.click(safe)

    expect($updateSafeModeAuto.get()).toBe(false)
    expect(safe.getAttribute('aria-checked')).toBe('false')
  })

  it('clicking the force-mode toggle flips its atom value without touching the other', (): void => {
    render(<UpdatesSettings />)

    const force = screen.getByRole('switch', { name: 'Allow Update Now when local is ahead' })
    fireEvent.click(force)

    expect($updateForceModeLocal.get()).toBe(true)
    expect($updateSafeModeAuto.get()).toBe(false)
  })

  it('exposes an aria-labelledby that points at the visible heading', (): void => {
    render(<UpdatesSettings />)

    const heading = screen.getByRole('heading', { name: 'Update behavior' })
    const section = heading.closest('section[aria-labelledby]')

    expect(section).toBeTruthy()
    expect(section?.getAttribute('aria-labelledby')).toBe(heading.id)
    expect(heading.id).toBe('updates-settings-heading')
  })
})
