import { describe, expect, it, vi } from 'vitest'

import { resolveRegistryRequestPath } from './registry-request-path'

// Regression cover for #118431/#118432 (Settings → 模型 showing the launch
// profile's model under every other profile's chip).
//
// This used to be a source-text grep over main.ts, which could only assert that
// certain strings existed somewhere in the function body. Now that the decision
// lives in its own module we assert the actual routing: which helper is called,
// with which arguments, for which kind of connection.

function deps() {
  return {
    pathForRegistryBackendRequest: vi.fn((path: string, profile: string | null) =>
      `/translated?path=${path}&profile=${profile ?? 'none'}`
    ),
    resolveProfileApiRequest: vi.fn(
      (profile: string | null, path: string, opts: unknown) => ({
        requestPath: `/scoped?path=${path}&profile=${profile ?? 'none'}&opts=${JSON.stringify(opts)}`
      })
    ),
    profileRouteOptions: vi.fn((profile: string | null) => ({ profile, scoped: true }))
  }
}

describe('resolveRegistryRequestPath (#settings-chip-model-scope)', () => {
  it('scopes the path through the v1 route table when the connection is the shared primary', () => {
    const d = deps()

    const result = resolveRegistryRequestPath(
      '/api/model/info',
      'research',
      { sharedPrimary: true },
      { method: 'GET', path: '/api/model/info' },
      d
    )

    expect(d.resolveProfileApiRequest).toHaveBeenCalledWith(
      'research',
      '/api/model/info',
      { profile: 'research', scoped: true }
    )
    expect(d.pathForRegistryBackendRequest).not.toHaveBeenCalled()
    expect(result).toContain('/scoped?')
    expect(result).toContain('profile=research')
  })

  it('builds route options from the request the same way the v1 handler does', () => {
    const d = deps()
    const request = { method: 'GET', path: '/api/model/info' }

    resolveRegistryRequestPath('/api/model/info', 'acme', { sharedPrimary: true }, request, d)

    expect(d.profileRouteOptions).toHaveBeenCalledWith('acme', request)
  })

  it.each([
    ['isolated', {}],
    ['ssh', {}],
    ['shared remote', {}]
  ])('keeps %s registry backends on pathForRegistryBackendRequest', (_label, scope) => {
    const d = deps()

    const result = resolveRegistryRequestPath('/api/model/info', 'acme', scope, {}, d)

    expect(d.pathForRegistryBackendRequest).toHaveBeenCalledWith('/api/model/info', 'acme', scope)
    expect(d.resolveProfileApiRequest).not.toHaveBeenCalled()
    expect(result).toContain('/translated?')
  })

  it('treats a null connection as non-shared', () => {
    const d = deps()

    resolveRegistryRequestPath('/api/model/info', null, null, {}, d)

    expect(d.pathForRegistryBackendRequest).toHaveBeenCalledWith('/api/model/info', null, null)
    expect(d.resolveProfileApiRequest).not.toHaveBeenCalled()
  })
})