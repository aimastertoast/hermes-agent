/**
 * The fork's bot-mode resume budget — FORK-OWNED.
 *
 * Hydrating a group member's session routes that session's MCP servers before
 * it answers, so on a multi-server profile the generic 30 s RPC default
 * reported a false timeout on a resume that completed anyway. The budget used
 * to be passed at the call sites (`group-compress.ts`, `group-turns.ts`) --
 * a fork delta on two upstream files that upstream rewrites, so a routine
 * merge conflicted and aborted `hermes update`. It lives here instead, at the
 * one place every bot request goes through.
 *
 * The room's sweep of the writes made outside the room (`group-external-writes.ts`)
 * used to be a THIRD call site, and it is the reason the budget cannot simply
 * be one constant: the sweep is hydration nobody waits on, so it must not take
 * the pool's reserved foreground slot to cold-start a member's backend, while
 * the room's "Compress history" is explicit user work and may. That split now
 * rides on the resume's own shape, so every call site is upstream's bytes
 * again.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { requestForBot } from './routing'
import type { ProfileRoute, RosterRow } from './types'

const { hostMock } = vi.hoisted(() => ({
  hostMock: {
    request: vi.fn(),
    requestProfile: vi.fn(),
    state: { connectionId: { get: vi.fn(() => 'local') } }
  }
}))

vi.mock('@hermes/plugin-sdk', () => ({ host: hostMock }))

const ROUTE: ProfileRoute = {
  connectionId: 'cloud-abc',
  mode: 'remote',
  profile: 'moxie',
  targetProfile: 'default'
}

const BOT = { name: 'moxie', route: ROUTE, sourceScoped: true } as RosterRow

/** The budget a group member's resume gets: long enough for MCP discovery,
 *  and a foreground spawn so a cold backend can take the pool's reserved
 *  slot (the room's "Compress history" is explicit user work). */
const RESUME_BUDGET_MS = 180_000

beforeEach(() => {
  hostMock.request.mockReset()
  hostMock.requestProfile.mockReset()
  hostMock.request.mockResolvedValue({})
  hostMock.requestProfile.mockResolvedValue({})
})

describe('requestForBot resume budget', () => {
  it('gives a session.resume the room resume budget when the caller gave none', async () => {
    await requestForBot(BOT, 'session.resume', { session_id: 's' })

    // A resume that pulls the transcript is hydration nobody waits on (the
    // room's mirror sweep), so it takes the budget WITHOUT the foreground
    // slot: the four-argument shape, no options object.
    expect(hostMock.requestProfile).toHaveBeenCalledWith(
      ROUTE,
      'session.resume',
      expect.anything(),
      RESUME_BUDGET_MS
    )
  })

  it('gives a transcript-free resume the foreground slot', async () => {
    // `omit_messages` means the caller only wants the runtime bound -- the
    // room's "Compress history", which the user is waiting on -- so its cold
    // backend spawn may take the pool's reserved foreground slot.
    await requestForBot(BOT, 'session.resume', { session_id: 's', omit_messages: true })

    expect(hostMock.requestProfile).toHaveBeenCalledWith(
      ROUTE,
      'session.resume',
      expect.anything(),
      RESUME_BUDGET_MS,
      { spawnPriority: 'foreground' }
    )
  })

  it('leaves other methods on the generic default (three-argument call shape)', async () => {
    await requestForBot(BOT, 'profiles.list', {})

    expect(hostMock.requestProfile).toHaveBeenCalledWith(ROUTE, 'profiles.list', expect.anything())
  })

  it('lets an explicit caller timeout win over the room budget', async () => {
    await requestForBot(BOT, 'session.resume', { session_id: 's' }, { timeoutMs: 5_000 })

    // With a timeout given but no spawnPriority, upstream keeps the four-
    // argument shape (no options object is appended) -- the caller's number
    // must reach the transport untouched.
    expect(hostMock.requestProfile).toHaveBeenCalledWith(ROUTE, 'session.resume', expect.anything(), 5_000)
  })
})
