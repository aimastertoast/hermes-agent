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
