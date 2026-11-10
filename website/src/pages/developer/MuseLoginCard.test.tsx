/**
 * Settings > Agent Harness > Muse: the in-product subscription login.
 *
 * The claims under test are the card's side of the device flow: Connect
 * starts the flow and shows the code plus the Meta link (never anything
 * else); polling folds a `ready` verdict into the signed-in state and
 * reports back through `onConnected`; cancel kills the flow. The server
 * half (spawn, parse, single-flight, owner gate) is pinned in
 * `test/test_muse_login.py` -- this file never re-asserts it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api, type MuseLoginStatus } from '../../api/client'
import { MuseLoginCard } from './MuseLoginCard'

function renderCard(onConnected?: () => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MuseLoginCard onConnected={onConnected} />
    </QueryClientProvider>,
  )
}

const GRANT: MuseLoginStatus = {
  state: 'pending',
  ok: true,
  rejoined: false,
  code: 'CLFM-PPVW',
  url: 'https://auth.meta.com/oauth/device/?code=CLFM-PPVW',
  detail: '',
}

/** The server's verdict, mutated per test to simulate the approval landing. */
let verdict: MuseLoginStatus

beforeEach(() => {
  verdict = { state: 'none', code: '', url: '', detail: '' }
  vi.spyOn(api, 'museLoginStart').mockImplementation(async () => ({ ...GRANT }))
  vi.spyOn(api, 'museLoginStatus').mockImplementation(async () => ({ ...verdict }))
  vi.spyOn(api, 'museLoginCancel').mockImplementation(async () => ({
    state: 'cancelled',
    code: '',
    url: '',
    detail: '',
  }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('MuseLoginCard', () => {
  it('connects and shows the code plus the Meta link', async () => {
    renderCard()
    fireEvent.click(screen.getByTestId('muse-login-connect'))
    await screen.findByText('CLFM-PPVW')
    const link = screen.getByTestId('muse-login-open')
    expect(link.getAttribute('href')).toBe('https://auth.meta.com/oauth/device/?code=CLFM-PPVW')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(api.museLoginStart).toHaveBeenCalledTimes(1)
  })

  it('folds a ready verdict into signed-in and reports back', async () => {
    const onConnected = vi.fn()
    renderCard(onConnected)
    fireEvent.click(screen.getByTestId('muse-login-connect'))
    await screen.findByText('CLFM-PPVW')
    verdict = { state: 'ready', code: '', url: '', detail: '' }
    // The verdict lands on the next 3s poll.
    await screen.findByTestId('muse-login-ready', undefined, { timeout: 8000 })
    await waitFor(() => expect(onConnected).toHaveBeenCalledTimes(1))
    // The grant leaves the screen with the verdict.
    expect(screen.queryByText('CLFM-PPVW')).toBeNull()
  })

  it('cancels a pending flow', async () => {
    renderCard()
    fireEvent.click(screen.getByTestId('muse-login-connect'))
    await screen.findByText('CLFM-PPVW')
    fireEvent.click(screen.getByTestId('muse-login-cancel'))
    await waitFor(() => expect(api.museLoginCancel).toHaveBeenCalledTimes(1))
  })

  it('shows a failed start with a retry', async () => {
    vi.mocked(api.museLoginStart).mockRejectedValueOnce(new Error('muse is not installed'))
    renderCard()
    fireEvent.click(screen.getByTestId('muse-login-connect'))
    await screen.findByTestId('muse-login-failed')
    await screen.findByText('muse is not installed')
    fireEvent.click(screen.getByTestId('muse-login-retry'))
    screen.getByTestId('muse-login-connect')
  })
})
