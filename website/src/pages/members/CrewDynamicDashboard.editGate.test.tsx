// @vitest-environment jsdom
/**
 * Edit is offered only over a page whose bridge can answer it. The capture layer
 * covers the frame, so over a page that runs no script (or one rendered without the
 * bridge) it would swallow every click and scroll with nothing to answer them.
 */
import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { api, type DashboardManifest } from '../../api/client'
import { renderWithProviders } from '../../test/helpers'
import CrewDynamicDashboard from './CrewDynamicDashboard'
import { EDIT_MESSAGE_TYPE } from './dashboardEdit'

vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? `/sandbox-doc/${srcdoc.length}` : null,
    pending: false,
    failed: false,
    stalled: false,
    retry: () => {},
  }),
}))
vi.mock('../../hooks/usePreviewFlag', () => ({ usePreviewFlag: () => true }))
vi.mock('../../hooks/usePreviewFlagServerSync', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../hooks/usePreviewFlagServerSync')>()),
  useServerPreviewFlag: () => true,
}))

const MANIFEST = { id: 'atlas-tree', version: 1, title: 'Tree', fields: [] } as unknown as DashboardManifest
const WITH_BRIDGE = `<!doctype html><p>v1</p><script>/* ${EDIT_MESSAGE_TYPE} */</script>`

function page(over: Record<string, unknown> = {}) {
  return {
    instance_version: 1,
    template: { id: 'atlas-tree', version: 1 },
    html: '<!doctype html><p>stored</p>',
    rendered_html: WITH_BRIDGE,
    manifest: MANIFEST,
    state: 'live' as const,
    ...over,
  }
}

const mount = () =>
  renderWithProviders(
    <CrewDynamicDashboard target={{ kind: 'member', slug: 'atlas', member: 'Atlas' }} displayName="Atlas" />,
  )

describe('the Edit button', () => {
  it('is offered over a page that runs script with the bridge in it', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    const edit = await screen.findByTestId('crew-dashboard-edit')
    // Named for what the mode does first: inspect where values came from.
    expect(edit).toHaveTextContent('Inspect')
    // Shown at rest, not only on hover: every use of edit mode starts here.
    expect(edit.className).not.toMatch(/opacity-0/)
  })

  it('is not offered when the page may not run script', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page({ scripts: false }))
    mount()
    await screen.findByTestId('crew-dashboard-iframe')
    expect(screen.queryByTestId('crew-dashboard-edit')).toBeNull()
  })
})
