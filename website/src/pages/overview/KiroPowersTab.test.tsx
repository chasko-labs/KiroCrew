/**
 * Customize > Kiro Powers lists every installed Power and Agent Plugin and says
 * plainly that none is loaded: a `POWER.md` Power reads as a legacy Power, a
 * valid plugin shows what it would add, an invalid one carries the loader's own
 * reason, and the list offers no switch. A non-owner is told the list is the
 * owner's in an error notice that hands the refusal to the agent, with no retry
 * that could never succeed. Each visit to the tab reads
 * the disk again, because Kiro IDE and kiro-cli change it behind the dashboard.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import { api, ApiError, type AgentPluginRecord, type AgentPluginsResponse } from '../../api/client'
import { renderWithProviders } from '../../test/helpers'
import {
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
  attachReport,
  consumeChatHandoff,
  installSoftNavigate,
  recordError,
} from '../../utils/errorReport'
import KiroPowersTab from './KiroPowersTab'

function rec(over: Partial<AgentPluginRecord> & { name: string }): AgentPluginRecord {
  return {
    state: 'off',
    reason: null,
    legacy_power: false,
    loaded: false,
    version: null,
    description: null,
    servers: [],
    skills: [],
    issues: [],
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
  __resetNavSeamForTests()
  __resetErrorJournalForTests()
  sessionStorage.clear()
})

const ownerRefusal = () =>
  new ApiError(403, 'dashboard owner required', JSON.stringify({ error: 'dashboard owner required', code: 'dashboard_owner_required' }))

/** The dashboard's own default: a query never goes stale by itself. */
const appQueryDefaults = { queryDefaults: { staleTime: Infinity } }

describe('KiroPowersTab', () => {
  it('lists every installed Power as not loaded, with its reason', async () => {
    vi.spyOn(api, 'agentPlugins').mockResolvedValue({
      plugins: [
        rec({ name: 'aws-docs', state: 'not-a-plugin', reason: 'legacy POWER.md power, not an Agent Plugin', legacy_power: true }),
        rec({ name: 'bare', state: 'not-a-plugin', reason: 'no plugin.json' }),
        rec({ name: 'broken', state: 'invalid', reason: 'plugin.json is not valid JSON' }),
        rec({
          name: 'good',
          version: '1.2.0',
          description: 'A valid plugin.',
          skills: [{ name: 'good-skill', description: 'Helps.' }],
          servers: [{ name: 'local', type: 'stdio', command: 'node' }],
          issues: [{ component: 'skills', entry: 'odd', message: 'the skill has no description' }],
        }),
      ],
    })
    renderWithProviders(<KiroPowersTab />)

    const rows = await screen.findAllByTestId('kiro-power-row')
    expect(rows).toHaveLength(4)
    for (const row of rows) {
      expect(within(row).getByTestId('kiro-power-not-loaded').textContent).toBe('Not loaded')
    }
    const reason = (i: number) => within(rows[i]).getByTestId('kiro-power-reason').textContent
    expect(reason(0)).toBe('A legacy POWER.md Power, not an Agent Plugin. Kiro Crew does not load POWER.md Powers.')
    expect(reason(1)).toBe('This folder has no plugin.json, so it is not an Agent Plugin.')
    expect(reason(2)).toBe('This plugin is invalid, so none of it can be used: plugin.json is not valid JSON')
    expect(reason(3)).toBe('A valid Agent Plugin. Kiro Crew does not load Agent Plugins yet.')

    const good = within(rows[3])
    expect(good.getByText('1.2.0')).toBeTruthy()
    expect(good.getByText('A valid plugin.')).toBeTruthy()
    expect(good.getByText('Skills it would add')).toBeTruthy()
    expect(good.getByText('good-skill')).toBeTruthy()
    expect(good.getByText('MCP servers it would add')).toBeTruthy()
    expect(good.getByText('local')).toBeTruthy()
    expect(good.getByText('Problems found')).toBeTruthy()
    expect(good.getByText(/the skill has no description/)).toBeTruthy()

    // Read-only: nothing on the tab turns a Power on.
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('says so when nothing is installed', async () => {
    vi.spyOn(api, 'agentPlugins').mockResolvedValue({ plugins: [] })
    renderWithProviders(<KiroPowersTab />)
    const empty = await screen.findByTestId('kiro-powers-empty')
    expect(empty.textContent).toContain('No Kiro Powers installed')
    expect(screen.queryAllByTestId('kiro-power-row')).toHaveLength(0)
  })

  it('tells a non-owner the list is the owner’s, without a retry', async () => {
    vi.spyOn(api, 'agentPlugins').mockRejectedValue(ownerRefusal())
    renderWithProviders(<KiroPowersTab />)
    const note = await screen.findByTestId('kiro-powers-owner-only')
    expect(note.textContent).toContain('Only the dashboard owner can see the installed Kiro Powers.')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('renders the owner refusal as an error notice that hands the refusal to the agent, still without a retry', async () => {
    // The client journals the 403 and pins that entry to the rejection; the
    // notice shows the owner-only sentence, so the entry has to ride along.
    const refused = attachReport(ownerRefusal(), recordError({
      source: 'api',
      message: 'dashboard owner required',
      status: 403,
      code: 'dashboard_owner_required',
      endpoint: '/api/agent-plugins',
    }))
    vi.spyOn(api, 'agentPlugins').mockRejectedValue(refused)
    installSoftNavigate(() => {})
    renderWithProviders(<KiroPowersTab />)

    const alert = await screen.findByRole('alert')
    expect(alert.getAttribute('data-testid')).toBe('kiro-powers-owner-only')
    expect(alert.textContent).toContain('Only the dashboard owner can see the installed Kiro Powers.')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()

    fireEvent.click(within(alert).getByRole('button', { name: 'Ask the agent' }))
    const prompt = consumeChatHandoff() ?? ''
    expect(prompt).toContain('/api/agent-plugins -> HTTP 403')
    expect(prompt).toContain('Code: dashboard_owner_required')
  })

  it('offers a retry when the read fails for another reason', async () => {
    const read = vi.spyOn(api, 'agentPlugins').mockRejectedValue(new ApiError(500, 'boom', '{"error":"boom"}'))
    renderWithProviders(<KiroPowersTab />)
    const retry = await screen.findByRole('button', { name: 'Retry' })
    read.mockResolvedValue({ plugins: [] })
    fireEvent.click(retry)
    await waitFor(() => expect(screen.getByTestId('kiro-powers-empty')).toBeTruthy())
  })

  it('reads the installed Powers again each time the tab opens', async () => {
    const read = vi.spyOn(api, 'agentPlugins').mockResolvedValue({ plugins: [] })
    const { rerender } = renderWithProviders(<KiroPowersTab />, appQueryDefaults)
    await screen.findByTestId('kiro-powers-empty')

    // Another tab, then back: a Power was installed with kiro-cli meanwhile.
    rerender(<div />)
    read.mockResolvedValue({ plugins: [rec({ name: 'installed-meanwhile' })] })
    rerender(<KiroPowersTab />)

    const rows = await screen.findAllByTestId('kiro-power-row')
    expect(rows).toHaveLength(1)
    expect(within(rows[0]).getByText('installed-meanwhile')).toBeTruthy()
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('keeps the owner-only note, never a Retry, while a non-owner’s visit reads again', async () => {
    const read = vi.spyOn(api, 'agentPlugins').mockRejectedValue(ownerRefusal())
    const { rerender } = renderWithProviders(<KiroPowersTab />, appQueryDefaults)
    await screen.findByTestId('kiro-powers-owner-only')

    rerender(<div />)
    let answer: (e: unknown) => void = () => {}
    read.mockReturnValue(new Promise<AgentPluginsResponse>((_resolve, reject) => { answer = reject }))
    rerender(<KiroPowersTab />)

    // The second read is in flight: the note stays and no Retry appears.
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('kiro-powers-owner-only')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()

    answer(ownerRefusal())
    await waitFor(() => expect(screen.getByTestId('kiro-powers-owner-only')).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })
})
