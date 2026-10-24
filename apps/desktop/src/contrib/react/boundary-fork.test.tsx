/**
 * The fork's contribution crash guard — FORK-OWNED.
 *
 * A contribution that throws on every render spun the renderer: the slot
 * re-rendered it, it threw again, the boundary caught it, repeat. The guard
 * counts the failures and locks the slot past the cap, so the broken subtree
 * is never rendered again.
 *
 * The guard's rules, pinned here:
 *  - an ordinary failure keeps upstream's fallback (a working Retry), so a
 *    single bad render never locks a slot;
 *  - a contribution that keeps failing is locked out, and the slot stops
 *    rendering it.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ContribBoundary } from './boundary'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

/** A contribution that throws on every render, counting its attempts. */
function throwingContribution() {
  const state = { renders: 0 }

  function Boom(): never {
    state.renders += 1
    throw new Error('plugin render failed')
  }

  return { Boom, state }
}

/** Click the chip's Retry until the slot is locked (or the cap is reached). */
function retryUntilLocked(name: string) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const retry = screen.queryByRole('button', { name })

    if (!retry) {
      return
    }

    fireEvent.click(retry)
  }
}

describe('the fork contribution crash guard', () => {
  it('leaves an ordinary failure on the ordinary fallback', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { Boom } = throwingContribution()

    render(
      <ContribBoundary id="one-off" variant="chip">
        <Boom />
      </ContribBoundary>
    )

    // Upstream's chip fallback: a plain button named after the contribution,
    // with a working retry. No lock, no post-mortem.
    expect(screen.getByRole('button', { name: 'one-off' })).toBeTruthy()
    expect(screen.queryByText(/locked/)).toBeNull()
  })

  it('locks a contribution that keeps crashing, and stops rendering it', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { Boom, state } = throwingContribution()

    const { rerender } = render(
      <ContribBoundary id="broken" variant="chip">
        <Boom />
      </ContribBoundary>
    )

    retryUntilLocked('broken')

    expect(screen.getByText('broken (locked)')).toBeTruthy()

    const rendersWhenLocked = state.renders

    rerender(
      <ContribBoundary id="broken" variant="chip">
        <Boom />
      </ContribBoundary>
    )

    expect(state.renders).toBe(rendersWhenLocked)
  })

  it('shows the pane post-mortem when a pane contribution is locked', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const { Boom } = throwingContribution()

    render(
      <ContribBoundary id="broken-pane" variant="pane">
        <Boom />
      </ContribBoundary>
    )

    for (let attempt = 0; attempt < 5; attempt += 1) {
      const retry = screen.queryByRole('button', { name: /retry/i })

      if (!retry) {
        break
      }

      fireEvent.click(retry)
    }

    expect(screen.getByText('«broken-pane» failed to render')).toBeTruthy()
  })
})
