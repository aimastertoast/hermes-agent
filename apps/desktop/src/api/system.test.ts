import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./client', () => ({
  capabilityScoped: vi.fn(),
  getApiRequestConnection: vi.fn(() => null),
  getApiRequestProfile: vi.fn(() => null),
  hermesApi: vi.fn(),
  profileScoped: vi.fn(() => ({}))
}))

const client = await import('./client')

const { getLastReceipt, acknowledgeReceipt } = await import('./system')

const hermesApi = vi.mocked(client.hermesApi)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(client.getApiRequestConnection).mockReturnValue(null)
  vi.mocked(client.getApiRequestProfile).mockReturnValue(null)
  vi.mocked(client.profileScoped).mockReturnValue({})
})

describe('getLastReceipt', () => {
  it('returns null when the backend reports no receipt', async () => {
    hermesApi.mockResolvedValue(null as never)

    const receipt = await getLastReceipt()

    expect(receipt).toBeNull()
    expect(hermesApi.mock.calls[0][0]).toMatchObject({
      path: '/api/hermes/update/receipt'
    })
  })

  it('returns the receipt when the backend serves one', async () => {
    const receipt = {
      acknowledged: false,
      error: 'merge-conflict',
      outcome: 'failed',
      post_state: { state_db_hash: 'def456' },
      pre_state: { state_db_hash: 'abc123' },
      receipt_id: 'r-2026-09-27-001',
      rolled_back: true,
      steps: [{ name: 'merge', ok: false, detail: ['conflict.txt'], warning: 'merge-conflict' }]
    }
    hermesApi.mockResolvedValue({ receipt } as never)

    const result = await getLastReceipt()

    expect(result).toEqual(receipt)
    expect(hermesApi.mock.calls[0][0]).toMatchObject({
      path: '/api/hermes/update/receipt'
    })
  })

  it('returns null when the response wraps no receipt (defensive)', async () => {
    // The backend may resolve with `{ receipt: null }` on a stale install; the
    // helper must not throw, it must return null.
    hermesApi.mockResolvedValue({ receipt: null } as never)

    const result = await getLastReceipt()

    expect(result).toBeNull()
  })
})

describe('acknowledgeReceipt', () => {
  it('POSTs to /api/hermes/update/receipt/<id>/ack and returns ok', async () => {
    hermesApi.mockResolvedValue({ ok: true } as never)

    const result = await acknowledgeReceipt('abc123')

    expect(result.ok).toBe(true)
    expect(hermesApi.mock.calls[0][0]).toMatchObject({
      method: 'POST',
      path: '/api/hermes/update/receipt/abc123/ack',
      body: {}
    })
  })

  it('URI-encodes the receipt id so path-significant characters do not escape the route', async () => {
    hermesApi.mockResolvedValue({ ok: true } as never)

    await acknowledgeReceipt('id/with/slashes')

    const req = hermesApi.mock.calls[0][0] as { path: string }
    expect(req.path).toBe('/api/hermes/update/receipt/id%2Fwith%2Fslashes/ack')
    expect(req.path).not.toContain('id/with/slashes/ack')
  })
})