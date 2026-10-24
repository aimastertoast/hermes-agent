import { QueryClientProvider } from '@tanstack/react-query'
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { getHermesConfigRecord } from '@/hermes'
import { queryClient } from '@/lib/query-client'
import { $settingsRequestProfile } from '@/store/settings-scope'

import { hermesConfigKey } from './use-config-record'
import { setHermesConfigCache, useHermesConfigRecord } from './use-config-record-fork'

// The settings scope is the store the settings pages read their "Applies to"
// chip from. Mocked so the test can point it at a profile without dragging
// the whole profile roster (and the app-wide active gateway) in.
vi.mock('@/store/settings-scope', async () => {
  const { atom } = await import('nanostores')

  return { $settingsRequestProfile: atom<string | undefined>('alpha') }
})

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getHermesConfigRecord: vi.fn()
}))

const wrapper = ({ children }: { children: React.ReactNode }) =>
  createElement(QueryClientProvider, { client: queryClient }, children)

// The real store is a computed (read-only); the mock's atom is writable, and
// the test has to move the scope.
const scope = $settingsRequestProfile as unknown as { set(value: string | undefined): void }

describe('the fork scoped config-record hooks', () => {
  beforeEach(() => {
    scope.set('alpha')
    vi.mocked(getHermesConfigRecord).mockResolvedValue({})
  })

  afterEach(() => {
    cleanup()
    queryClient.clear()
    vi.clearAllMocks()
  })

  it('reads the record under the settings scope, not the active profile', async () => {
    renderHook(() => useHermesConfigRecord(), { wrapper })

    await waitFor(() => expect(getHermesConfigRecord).toHaveBeenCalledWith('alpha'))
  })

  it('writes the cache under the settings scope, not the active gateway row', () => {
    const record = { desktop: { font_family: 'OpenDyslexic' } }

    setHermesConfigCache(record)

    expect(queryClient.getQueryData(hermesConfigKey('alpha'))).toEqual(record)
    // The unscoped slot is the ACTIVE gateway's row. A settings save that
    // lands there paints the edited profile's record into the active profile
    // (the B1 class).
    expect(queryClient.getQueryData(hermesConfigKey())).toBeUndefined()
  })

  it('writes to the profile the record was read from, not the scope at write time', () => {
    // The settings scope moved while the save was in flight. The record
    // belongs to the profile it was READ from -- the read origin says which
    // one that is. Painting it into the scope's current row would put one
    // profile's whole record into another profile's row.
    setHermesConfigCache({ desktop: { font_family: 'Lexend' } }, { profile: 'beta' })

    expect(queryClient.getQueryData(hermesConfigKey('beta'))).toEqual({ desktop: { font_family: 'Lexend' } })
    expect(queryClient.getQueryData(hermesConfigKey('alpha'))).toBeUndefined()
  })
})
