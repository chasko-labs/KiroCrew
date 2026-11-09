/**
 * InstancesViewport in the desktop app's client-only mode: the dashboard is a
 * crew's gateway on another machine, and each pane frames a port that gateway
 * forwarded on ITS machine. The app forwards those ports on demand
 * (`window.remoteCrewPaneAPI`), so a pane's frame must wait for its port before
 * it loads, and a port no pane shows any more must be released.
 *
 * The mocks are copies of InstancesViewport.paneLoad.test.tsx's: a vi.mock
 * factory applies only to the file it is written in.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { removeWarm } from '../store/instancesSlice'
import { mockConnectedCd1 } from './InstancesViewportTestUtils'

vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  isElectron: true,
}))

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    updateInstance: vi.fn().mockResolvedValue({}),
    addInstance: vi.fn().mockResolvedValue({ id: 'added-1' }),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
  },
}))

type Answer = Record<string, 'up' | 'down'> | null

/** A bridge whose every answer the test releases by hand. */
function installBridge() {
  const answers: Array<(answer: Answer) => void> = []
  const forwardPorts = vi.fn((_ports: number[]) => new Promise<Answer>(resolve => { answers.push(resolve) }))
  window.remoteCrewPaneAPI = { forwardPorts }
  return { forwardPorts, answers }
}

const warmCd1 = () =>
  createTestStore({
    instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
  })

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  mockConnectedCd1()
})

afterEach(() => {
  vi.useRealTimers()
  delete window.remoteCrewPaneAPI
})

describe('InstancesViewport', () => {
  it('asks the desktop app to forward a pane port and loads the pane only once it is tried', async () => {
    const { forwardPorts, answers } = installBridge()
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })

    await waitFor(() => expect(forwardPorts).toHaveBeenCalledWith([7778]))
    // Mounting the frame before the forward exists would load a refused port.
    expect(document.querySelector('iframe')).toBeNull()
    expect(screen.getByText(/Loading pane/i)).toBeInTheDocument()

    await act(async () => { answers[answers.length - 1]({ 7778: 'up' }) })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe('http://localhost:7778/?token=tok')
  })

  it('loads a pane whose forward failed, so the watchdog can explain it', async () => {
    const { forwardPorts, answers } = installBridge()
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })
    await waitFor(() => expect(forwardPorts).toHaveBeenCalledWith([7778]))
    await act(async () => { answers[answers.length - 1]({ 7778: 'down' }) })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
  })

  it('loads the pane at once when the bridge refuses the window', async () => {
    const forwardPorts = vi.fn(() => Promise.reject(new Error('restricted to the launch port')))
    window.remoteCrewPaneAPI = { forwardPorts }
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    expect(forwardPorts).toHaveBeenCalled()
  })

  it('mounts the pane at once with no desktop bridge, as in a browser', () => {
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe('http://localhost:7778/?token=tok')
  })

  it('does not arm the load watchdog while the forward is still being tried', async () => {
    const { forwardPorts, answers } = installBridge()
    vi.useFakeTimers()
    renderWithProviders(<InstancesViewport />, { store: warmCd1() })
    await act(async () => { await Promise.resolve() })
    expect(forwardPorts).toHaveBeenCalledWith([7778])
    // A forward through a slow ProxyCommand can take most of ssh's budget.
    await act(async () => { vi.advanceTimersByTime(15_000) })
    expect(screen.queryByText(/Pane failed to load/i)).toBeNull()

    await act(async () => { answers[answers.length - 1]({ 7778: 'up' }) })
    // The countdown starts when the frame does.
    await act(async () => { vi.advanceTimersByTime(14_000) })
    expect(screen.queryByText(/Pane failed to load/i)).toBeNull()
    await act(async () => { vi.advanceTimersByTime(1_000) })
    expect(screen.getByText(/Pane failed to load/i)).toBeInTheDocument()
  })

  it('releases a pane port once no pane shows it', async () => {
    const { forwardPorts, answers } = installBridge()
    const store = warmCd1()
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(forwardPorts).toHaveBeenCalledWith([7778]))
    await act(async () => { answers[answers.length - 1]({ 7778: 'up' }) })

    act(() => { store.dispatch(removeWarm('cd-1')) })
    await waitFor(() => expect(forwardPorts).toHaveBeenLastCalledWith([]))
  })
})
