import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AccountRebindView from './AccountRebindView.vue'

function reply(body: unknown, ok = true) {
  return {
    ok,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response
}

afterEach(() => vi.unstubAllGlobals())

describe('AccountRebindView start controls', () => {
  it('starts pending account items even when a stale task-level status says failed', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/account-rebind/tasks' && !init?.method) {
        return reply({ items: [{ taskId: 'task-1', status: 'failed', progress: 0, items: [{ accountId: 'account-1', email: 'pending@example.test', status: 'pending', progress: 0 }] }] })
      }
      if (url === '/api/account-rebind/pools') return reply({ concurrency: 2, availableStandardEmails: 10, success: [] })
      if (url === '/api/account-rebind/proxies') return reply({
        countries: [{ country: 'GB', total: 1, enabled: 1, rebindAvailable: 1 }],
        groups: [{ country: 'GB', group: 'yaml', total: 1, enabled: 1, available: 1, quarantined: 0, schemes: ['http'], rebindAvailable: 1 }],
      })
      if (url === '/api/account-rebind/logs') return reply({ items: [] })
      if (url === '/api/account-rebind/concurrency' && init?.method === 'PUT') return reply({ concurrency: 2 })
      if (url === '/api/account-rebind/tasks/start' && init?.method === 'POST') return reply({ requested: 1, started: 1, failed: 0, concurrency: 2 })
      throw new Error(`Unexpected request: ${init?.method || 'GET'} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(AccountRebindView, {
      global: { plugins: [ElementPlus], mocks: { $router: { push: vi.fn() } } },
    })
    await flushPromises()

    const startButton = wrapper.findAll('button').find((button) => button.text().includes('按当前设置开始换绑'))
    expect(startButton).toBeDefined()
    expect(startButton!.attributes('disabled')).toBeUndefined()
    expect(startButton!.text()).toContain('（1）')

    await startButton!.trigger('click')
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/account-rebind/tasks/start',
      expect.objectContaining({ method: 'POST' }),
    )
    wrapper.unmount()
  })
})
