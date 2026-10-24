/**
 * The fork's transport-level request budget — FORK-OWNED.
 *
 * The fork used to pass SESSION_RESUME_REQUEST_TIMEOUT_MS at every
 * `session.resume` call site. That put a fork delta on nine upstream files
 * (each of which upstream rewrites), so a routine upstream merge conflicted
 * and aborted `hermes update` with the GUI's "Update failed (exit 1)"
 * banner. The budget now lives at the transport, once, and the call sites are
 * upstream's bytes again.
 *
 * The bug it fixes: `session.resume` is the RPC behind the composer's
 * "Continue". It has to route the session's MCP servers before it answers, and
 * on a multi-server install that discovery legitimately runs for minutes — the
 * generic 30 s default rejected resumes that then completed underneath the
 * user, so the toast was a false failure, never a connectivity fault.
 */
import { JsonRpcGatewayClient } from '@hermes/shared'
import { afterEach, expect, test, vi } from 'vitest'

import { gatewayRequestTimeout, HermesGateway, SESSION_RESUME_REQUEST_TIMEOUT_MS } from './client'

afterEach(() => { vi.restoreAllMocks() })

test('session.resume rides the resume budget instead of the generic gateway default', () => {
  expect(gatewayRequestTimeout('session.resume', undefined)).toBe(SESSION_RESUME_REQUEST_TIMEOUT_MS)
  // The generic default was 30 s; anything at or below it reintroduces the
  // false "Continue failed" toast.
  expect(SESSION_RESUME_REQUEST_TIMEOUT_MS).toBeGreaterThan(30_000)
})

test('other methods keep the generic default, and a caller timeout wins', () => {
  expect(gatewayRequestTimeout('prompt.submit', undefined)).toBeUndefined()
  expect(gatewayRequestTimeout('session.resume', 7_000)).toBe(7_000)
})

test('the transport spends the resume budget on a session.resume with no explicit timeout', async () => {
  let now = 1_000
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  vi.spyOn(JsonRpcGatewayClient.prototype, 'connect').mockResolvedValue()
  const calls: Array<{ method: string; timeout?: number }> = []
  vi.spyOn(JsonRpcGatewayClient.prototype, 'request').mockImplementation(async (method, _params, timeout) => {
    calls.push({ method, timeout })

    return { session_id: 's', revision: 1, execution_generation: 2, messages: [], info: {} } as never
  })
  const client = new HermesGateway()
  await client.connect('ws://localhost/api/ws?native_dial=fixture&ticket=fixture')
  await client.request('session.resume', { session_id: 's', profile: 'sibling' })
  // Every resume on this call rides the resume budget -- the attach
  // prerequisite included, so no leg of it can die at the generic default.
  expect(calls.filter(call => call.method === 'session.resume').length).toBeGreaterThan(0)
  expect(calls.filter(call => call.method === 'session.resume').every(call => call.timeout === SESSION_RESUME_REQUEST_TIMEOUT_MS)).toBe(true)
  client.close()
})
