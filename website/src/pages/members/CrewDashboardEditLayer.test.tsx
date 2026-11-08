// @vitest-environment jsdom
/**
 * Dashboard edit mode over a Bot's sandboxed page: the host turns Edit on in the frame,
 * refuses a path the page does not declare, draws the gateway's lineage for one it does,
 * and sends a change request only from the person's own Send.
 */
import { useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api, type DashboardLineage } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { renderWithProviders } from '../../test/helpers'
import CrewDashboardEditLayer from './CrewDashboardEditLayer'
import { MEMBER_DASHBOARD_QUERY_PREFIX } from '../../hooks/useWebSocket'

/** The field-push listeners the layer subscribes, so a case can deliver a push. */
const pushes = vi.hoisted(() => [] as ((read: Record<string, unknown>, dashboard: string) => void)[])
vi.mock('../../hooks/useWebSocket', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../hooks/useWebSocket')>()),
  subscribeDashboardFields: (_slug: string, fn: (read: Record<string, unknown>, dashboard: string) => void) => {
    pushes.push(fn)
    return () => { pushes.splice(pushes.indexOf(fn), 1) }
  },
}))
import { ACTIVATE_MESSAGE_TYPE, EDIT_KEY_MESSAGE_TYPE, EDIT_MESSAGE_TYPE, HOVER_LEAVE_MS, PICKED_MESSAGE_TYPE, holdDuration } from './dashboardEdit'

const MANIFEST = {
  id: 'project-report', version: 1, title: '', description: '', source: 'builtin',
  fields: {
    accepted: { type: 'number', source: { fold: 'workstreams', path: 'accepted' } },
    verdict: { type: 'string', source: { agentic: true } },
  },
}

const LINEAGE: DashboardLineage = {
  path: 'accepted', field: 'accepted', kind: 'fold', type: 'number', found: true, value: 7,
  hops: [
    { kind: 'field', field: 'accepted', type: 'number', fold: 'workstreams', path: 'accepted' },
    { kind: 'fold', fold: 'workstreams', path: 'accepted', found: true, by: 'gateway' },
    { kind: 'crew_log', fold: 'workstreams', needs: 'entry seq on fold rows' },
  ],
  edit: { kind: 'fold' },
}

function Host({ onSend, version = 14, hidden = false }: { onSend?: (text: string) => Promise<boolean>; version?: number; hidden?: boolean }) {
  const ref = useRef<HTMLIFrameElement | null>(null)
  return (
    <div className="relative" hidden={hidden}>
      <iframe ref={ref} title="page" data-testid="frame" />
      <CrewDashboardEditLayer
        frameRef={ref}
        frameKey="k"
        slug="atlas"
        member="Atlas"
        botName="Atlas"
        manifest={MANIFEST}
        board="project-report"
        instanceVersion={version}
        onSend={onSend}
        onDone={() => {}}
      />
    </div>
  )
}

function frameWindow(): Window {
  return (screen.getByTestId('frame') as HTMLIFrameElement).contentWindow as Window
}

/** The frame's answer for keyboard focus (`seq: -1`). The hover dwell is waited out
 *  by each case's `findBy*`, which polls. */
function pick(data: Record<string, unknown>) {
  act(() => {
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: PICKED_MESSAGE_TYPE, seq: -1, rect: { x: 10, y: 10, w: 30, h: 20 }, block: '', blockRect: null, blockFields: [], ...data },
      source: frameWindow(),
    }))
  })
}

/** Tap the capture layer and answer the point it asks for, as the frame would. */
function tap(answer: Record<string, unknown>) {
  const posted: { type?: string; seq?: number }[] = []
  const spy = vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m as { seq?: number }) })
  fireEvent.click(screen.getByTestId('dashboard-edit-capture'), { clientX: 20, clientY: 20 })
  spy.mockRestore()
  const point = posted.find(m => m.type === 'kirocrew-dashboard:point')
  expect(point?.seq).toBeGreaterThan(0)
  pick({ seq: point?.seq, ...answer })
}

afterEach(() => vi.restoreAllMocks())

describe('CrewDashboardEditLayer', () => {
  it('turns Edit on in the frame it draws over', async () => {
    const posted: unknown[] = []
    renderWithProviders(<Host />)
    vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m) })
    act(() => { window.dispatchEvent(new MessageEvent('message', { data: { type: 'kirocrew-dashboard:ready' }, source: frameWindow() })) })
    expect(posted).toContainEqual({ type: EDIT_MESSAGE_TYPE, on: true })
  })

  it('refuses a path the page does not declare and asks the gateway nothing', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage')
    renderWithProviders(<Host />)
    pick({ path: 'secret' })
    const refused = await screen.findByTestId('dashboard-edit-refused')
    expect(refused).toHaveTextContent('secret')
    // A hint from the host's own check, not a failed request: no alert is raised.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(lineage).not.toHaveBeenCalled()
  })

  it('draws the lineage the gateway built for a declared path', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    expect(await screen.findByTestId('lineage-popover')).toBeInTheDocument()
    expect(lineage).toHaveBeenCalledWith('atlas', 'Atlas', 'accepted')
    expect(await screen.findByTestId('lineage-value')).toHaveTextContent('7')
    expect(screen.getByTestId('lineage-hop-crew_log').querySelector('[title="entry seq on fold rows"]')).not.toBeNull()
    // The actions are pinned to the popover's bottom, so a long lineage never scrolls
    // them out of view.
    expect(screen.getByTestId('lineage-edit')).toHaveClass('sticky')
    // A folded value: Edit offers "change how it's shown"; a hand correction is greyed.
    fireEvent.click(screen.getByTestId('lineage-edit-open'))
    expect(screen.getByTestId('lineage-ask')).toBeInTheDocument()
    expect(screen.getByTestId('lineage-correct-greyed')).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByTestId('lineage-correct')).toBeNull()
  })

  it('pins the popover while Edit is open, and Esc closes Edit before the popover', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-value')
    fireEvent.keyDown(window, { key: 'e' })
    expect(await screen.findByTestId('lineage-edit-menu')).toBeInTheDocument()
    // Another value under the pointer does not replace a pinned popover.
    pick({ path: 'verdict' })
    pick({ path: null, rect: null })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(screen.getByTestId('lineage-edit-menu')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('lineage-edit-menu')).toBeNull()
    expect(screen.getByTestId('lineage-popover')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('lineage-popover')).toBeNull()
  })

  it("asks before it runs a script's job", async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [{ kind: 'freshness', max_age_s: 600, stale_after: '', by: 'script' }],
      edit: { kind: 'source', writer: 'cron:p99', job: 'p99' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [] })
    const run = vi.spyOn(api, 'runCron').mockResolvedValue({ ok: true })
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-source'))
    // The view is named for what the button promised: a look at the job, not an editor.
    expect(screen.getByTestId('lineage-job-heading')).toHaveTextContent('The job behind this value')
    // The hold reads as a duration, never as a bare count of seconds.
    expect(screen.getByTestId('lineage-source-box')).toHaveTextContent(`Counts as current for${holdDuration(600)}`)
    expect(screen.getByTestId('lineage-source-box')).not.toHaveTextContent('600')
    fireEvent.click(await screen.findByTestId('lineage-run'))
    expect(run).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('lineage-run-yes'))
    await waitFor(() => expect(run).toHaveBeenCalledWith('p99'))
  })

  it('starts one run for a double-clicked confirm and keeps saying it started', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [{ kind: 'freshness', max_age_s: 600, stale_after: '', by: 'script' }],
      edit: { kind: 'source', writer: 'cron:p99', job: 'p99' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [] })
    let finish: (v: unknown) => void = () => {}
    const run = vi.spyOn(api, 'runCron')
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
      .mockRejectedValue(new Error('409 already running'))
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-source'))
    fireEvent.click(await screen.findByTestId('lineage-run'))
    const yes = screen.getByTestId('lineage-run-yes')
    fireEvent.click(yes)
    fireEvent.click(yes)
    await act(async () => { finish({ ok: true }); await Promise.resolve() })
    expect(run).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/Run started/)).toBeInTheDocument()
    expect(screen.queryByText('The run could not start.')).toBeNull()
  })

  it('flips a component and sends the request only on the person\'s Send', async () => {
    const onSend = vi.fn().mockResolvedValue('sent')
    renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted', 'verdict', 'undeclared'] })
    const provider = await screen.findByTestId('dashboard-edit-provider')
    // Titled by what the card does: a change request, not a look at where data comes from.
    expect(screen.getByTestId('dashboard-edit-flip')).toHaveTextContent('Change tasks')
    expect(provider).toHaveTextContent('accepted')
    expect(provider).toHaveTextContent('verdict')
    expect(provider).not.toHaveTextContent('undeclared')
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
    expect(onSend.mock.calls[0][0]).toBe('show CI state\n\n[Dashboard change request · board project-report · component tasks · field accepted · page v14]')
  })

  it('keeps naming the page version a request was opened on when a new version lands', async () => {
    const onSend = vi.fn().mockResolvedValue('sent')
    const { rerender } = renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    await screen.findByTestId('dashboard-edit-provider')
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    // The Bot applies v15 while the draft is open; the layer stays mounted.
    rerender(<Host onSend={onSend} version={15} />)
    expect(screen.getByTestId('dashboard-edit-flip')).toHaveTextContent('v14')
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
    expect(onSend.mock.calls[0][0]).toContain('· page v14]')
  })

  it('turns the card over from the component it replaces, then moves it into place', async () => {
    // jsdom lays nothing out: give the pane and the card a size.
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(800)
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(600)
    const frames: FrameRequestCallback[] = []
    renderWithProviders(<Host onSend={vi.fn()} />)
    // Held frames from here: the card mounts inside the tap's answer.
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length })
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 40, y: 60, w: 680, h: 200 }, blockFields: ['accepted'] })
    await screen.findByTestId('dashboard-edit-flip')
    const shell = screen.getByTestId('flip-card-shell')
    // The first frame sits on the component's box, scaled to its size.
    await waitFor(() => expect(shell.style.transform).toMatch(/^translate\(0px, 0px\) scale\(/))
    expect(shell.style.transition).toBe('none')
    let ran = 0
    act(() => { while (frames.length) { frames.shift()?.(0); ran++ } })
    expect(ran).toBeGreaterThan(1)
    expect(shell.style.transform).toBe('none')
    expect(shell.style.transition).toMatch(/^transform 260ms/)
  })

  it('swaps to the card at once under reduced motion', async () => {
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(800)
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(600)
    vi.spyOn(window, 'matchMedia').mockImplementation((q: string) => ({
      matches: q.includes('reduce'), media: q, onchange: null,
      addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
    }) as MediaQueryList)
    renderWithProviders(<Host onSend={vi.fn()} />)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 40, y: 60, w: 680, h: 200 }, blockFields: ['accepted'] })
    const card = await screen.findByTestId('dashboard-edit-flip')
    const shell = screen.getByTestId('flip-card-shell')
    expect(shell.style.transform).toBe('none')
    expect(shell.style.transition).toBe('none')
    expect(card.style.transform).toBe('rotateY(0deg)')
  })

  it('flips the focused component on Enter, the keyboard path to the same card', async () => {
    renderWithProviders(<Host />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    pick({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: ACTIVATE_MESSAGE_TYPE }, source: frameWindow() }))
    })
    expect(await screen.findByTestId('dashboard-edit-provider')).toHaveTextContent('accepted')
  })

  it('moves the focus into an editor a frame key opened, and back to the frame on Esc', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-edit-open')
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: EDIT_KEY_MESSAGE_TYPE }, source: frameWindow() }))
    })
    // Edit opened from the frame holds the focus in the host, on its first option.
    await waitFor(() => expect(screen.getByTestId('lineage-edit')).toContainElement(document.activeElement as HTMLElement))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('lineage-ask')).toBeNull()
    expect(document.activeElement).toBe(screen.getByTestId('frame'))
  })

  it('focuses the request box of a component the frame opened with Enter', async () => {
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: ACTIVATE_MESSAGE_TYPE }, source: frameWindow() }))
    })
    const box = await screen.findByTestId('dashboard-edit-comment')
    expect(document.activeElement).toBe(box)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('dashboard-edit-flip')).toBeNull()
    expect(document.activeElement).toBe(screen.getByTestId('frame'))
  })

  it('leaves an "e" typed into the chat composer alone', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-edit-open')
    const composer = document.createElement('div')
    composer.contentEditable = 'true'
    // jsdom does not compute isContentEditable from the attribute.
    Object.defineProperty(composer, 'isContentEditable', { value: true })
    document.body.appendChild(composer)
    const typed = new KeyboardEvent('keydown', { key: 'e', bubbles: true, cancelable: true })
    act(() => { composer.dispatchEvent(typed) })
    expect(typed.defaultPrevented).toBe(false)
    expect(screen.queryByTestId('lineage-edit-menu')).toBeNull()
    composer.remove()
  })

  it('keeps the popover open when the frame\'s "nothing here" arrives after the pointer entered it', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    const popover = await screen.findByTestId('lineage-popover')
    vi.useFakeTimers()
    try {
      fireEvent.mouseEnter(popover)
      // The late reply to the pointer leaving the value for the popover.
      pick({ path: null, rect: null })
      act(() => { vi.advanceTimersByTime(HOVER_LEAVE_MS + 1) })
      expect(screen.getByTestId('lineage-popover')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-reads an open lineage when the page\'s values are pushed', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-value')
    expect(lineage).toHaveBeenCalledTimes(1)
    expect(pushes.length).toBeGreaterThan(0)
    act(() => { for (const fn of [...pushes]) fn({ fields: { accepted: 8 } }, '') })
    await waitFor(() => expect(lineage).toHaveBeenCalledTimes(2))
  })

  it("says a script job's failed last run through the shared error notice", async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE,
      kind: 'custom',
      hops: [],
      edit: { kind: 'source', writer: 'cron:p99', job: 'p99' },
    })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [{ id: 'p99', name: 'p99', last_error: 'throttled' }] } as never)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-source'))
    expect(await screen.findByTestId('lineage-job-failed')).toHaveTextContent('throttled')
  })

  it('opens what is under a tap that had no hover before it, not a stale pick', async () => {
    renderWithProviders(<Host onSend={vi.fn()} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    // No pointer move: the only pick is the tap's own answer.
    tap({ path: 'verdict', block: 'status', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['verdict'] })
    const provider = await screen.findByTestId('dashboard-edit-provider')
    expect(provider).toHaveTextContent('verdict')
    expect(provider).not.toHaveTextContent('accepted')
  })

  it("keeps another component's draft when an earlier send completes after its card closed", async () => {
    let finish: (outcome: 'sent') => void = () => {}
    const onSend = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    // Component A: send, then close its card while the send is still out.
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'first' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('dashboard-edit-comment')).toBeNull())
    // Component B: a new draft.
    tap({ path: 'verdict', block: 'status', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['verdict'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'second draft' } })
    vi.useFakeTimers()
    try {
      await act(async () => { finish('sent'); await Promise.resolve() })
      act(() => { vi.advanceTimersByTime(2000) })
      expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('second draft')
    } finally {
      vi.useRealTimers()
    }
  })

  it('says a gateway refusal of a correction through the error notice, not the type hint', async () => {
    vi.spyOn(api, 'memberDashboardCorrect').mockRejectedValue(new ApiError(400, 'wrong type', JSON.stringify({ code: 'wrong_type', error: 'wrong type' })))
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    fireEvent.change(screen.getByLabelText(/value/i), { target: { value: 'green' } })
    await act(async () => { fireEvent.click(screen.getByTestId('dashboard-edit-correct-save')) })
    expect(await screen.findByTestId('dashboard-edit-correct-failed')).toBeInTheDocument()
    expect(screen.getByTestId('dashboard-edit-correct-hint')).toHaveTextContent('')
    expect(screen.getByLabelText(/value/i)).toHaveValue('green')
  })

  it('keeps a correction draft when a refresh rebinds the field, with Save off', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    const { queryClient } = renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    const box = screen.getByTestId('dashboard-edit-correct')
    fireEvent.change(box.querySelectorAll('input')[0], { target: { value: 'green' } })
    // The Bot applies a page that makes the field counted; the open lineage re-reads.
    lineage.mockResolvedValue({ ...LINEAGE, path: 'verdict', field: 'verdict' })
    await act(async () => { await queryClient.refetchQueries({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas', 'Atlas', 'lineage', 'verdict'] }) })
    await waitFor(() => expect(screen.getByTestId('dashboard-edit-correct-save')).toBeDisabled())
    expect(screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')[0]).toHaveValue('green')
    expect(screen.getByTestId('dashboard-edit-correct-hint')).toHaveTextContent(/counted from/i)
  })

  it('saves an empty correction to clear a string value', async () => {
    const correct = vi.spyOn(api, 'memberDashboardCorrect').mockResolvedValue({ ok: true } as never)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    // The shared Input, so both fields carry the app's focus ring.
    for (const field of screen.getByTestId('dashboard-edit-correct').querySelectorAll('input')) expect(field).toHaveClass('focus-ring')
    const save = screen.getByTestId('dashboard-edit-correct-save')
    expect(save).toBeEnabled()
    await act(async () => { fireEvent.click(save) })
    expect(correct).toHaveBeenCalledWith('atlas', 'Atlas', 'verdict', '', '')
  })

  it('keeps a newer Ask draft when an earlier correction completes after its box closed', async () => {
    let finish: (v: unknown) => void = () => {}
    vi.spyOn(api, 'memberDashboardCorrect').mockImplementation(() => new Promise(resolve => { finish = resolve }) as never)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue({
      ...LINEAGE, path: 'verdict', field: 'verdict', kind: 'agentic', type: 'string',
      hops: [], edit: { kind: 'correct', type: 'string', scalar: true },
    })
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'verdict' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-correct'))
    fireEvent.change(screen.getByLabelText(/value/i), { target: { value: 'green' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-correct-save'))
    // Esc closes the correction box; then Ask is opened and a draft typed.
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'keep me' } })
    await act(async () => { finish({ ok: true }); await Promise.resolve() })
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('keep me')
  })

  it('locks the request text from Send until the card closes', async () => {
    const onSend = vi.fn().mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    const box = await screen.findByTestId('dashboard-edit-comment')
    fireEvent.change(box, { target: { value: 'first' } })
    expect(box).not.toHaveAttribute('readonly')
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    await waitFor(() => expect(screen.getByTestId('dashboard-edit-comment')).toHaveAttribute('readonly'))
  })

  it('opens Edit when the frame forwards E from a focused value', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-edit-open')
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: EDIT_KEY_MESSAGE_TYPE }, source: frameWindow() }))
    })
    expect(await screen.findByTestId('lineage-edit-menu')).toBeInTheDocument()
  })

  it('ignores a frame Esc or click that would discard an open draft', async () => {
    renderWithProviders(<Host onSend={vi.fn()} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'unsent' } })
    // What a page script can post: the same messages the bridge sends.
    for (const type of ['kirocrew-dashboard:escape', ACTIVATE_MESSAGE_TYPE, EDIT_KEY_MESSAGE_TYPE]) {
      act(() => { window.dispatchEvent(new MessageEvent('message', { data: { type }, source: frameWindow() })) })
    }
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('unsent')
    // The person's own Esc in the host still closes it.
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('dashboard-edit-comment')).toBeNull())
  })

  it('ignores a frame Esc while Edit is open in the popover', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'unsent' } })
    act(() => { window.dispatchEvent(new MessageEvent('message', { data: { type: 'kirocrew-dashboard:escape' }, source: frameWindow() })) })
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('unsent')
  })

  it('does not offer a resend when the send was not confirmed', async () => {
    const onSend = vi.fn().mockResolvedValue('unconfirmed')
    renderWithProviders(<Host onSend={onSend} />)
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    tap({ path: 'accepted', block: 'tasks', blockRect: { x: 0, y: 0, w: 300, h: 200 }, blockFields: ['accepted'] })
    fireEvent.change(await screen.findByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    fireEvent.click(screen.getByTestId('dashboard-edit-send'))
    expect(await screen.findByTestId('dashboard-edit-send-unconfirmed')).toBeInTheDocument()
    expect(screen.getByTestId('dashboard-edit-send')).toBeDisabled()
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('paints a field picked from Sources over the Sources list that stays open beside it', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    // jsdom lays nothing out, so the panel's positioned ancestor is supplied.
    vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockImplementation(function (this: HTMLElement) { return this.parentElement })
    renderWithProviders(<Host />)
    fireEvent.click(screen.getByTestId('dashboard-edit-sources-button'))
    fireEvent.click(screen.getByTestId('dashboard-edit-source-accepted'))
    const popover = await screen.findByTestId('lineage-popover')
    const sources = screen.getByTestId('dashboard-edit-sources')
    const z = (el: HTMLElement) => Number(getComputedStyle(el).zIndex)
    expect(z(popover)).toBeGreaterThan(z(sources))
    expect(z(screen.getByTestId('lineage-connector') as unknown as HTMLElement)).toBeGreaterThan(z(sources))
  })

  it('says where a value came from in one sentence and keeps the steps behind Details', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    expect(await screen.findByTestId('lineage-summary')).toHaveTextContent("Counted from Atlas's activity.")
    const details = screen.getByTestId('lineage-details')
    expect(details).not.toHaveAttribute('open')
    expect(details).toContainElement(screen.getByTestId('lineage-hop-crew_log'))
  })

  it('offers one control to change a value, named for what it does', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    expect(await screen.findByTestId('lineage-edit-open')).toHaveTextContent('Change this…')
    expect(screen.queryByTestId('lineage-pencil')).toBeNull()
  })

  it('names every step in words a reader knows, open Details included', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    const popover = await screen.findByTestId('lineage-popover')
    await screen.findByTestId('lineage-summary')
    expect(popover.textContent).not.toMatch(/\bfold\b|gateway|needs:|cannot write/i)
    expect(screen.getByTestId('lineage-hop-crew_log')).toHaveTextContent('Not tracked yet')
  })

  it('keeps the pinned popover when a hover was still dwelling as Edit opened', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host onSend={vi.fn()} />)
    pick({ path: 'accepted' })
    const open = await screen.findByTestId('lineage-edit-open')
    vi.useFakeTimers()
    try {
      pick({ path: 'verdict' })
      fireEvent.click(open)
      act(() => { vi.advanceTimersByTime(1000) })
    } finally {
      vi.useRealTimers()
    }
    expect(screen.getByTestId('lineage-ask')).toBeInTheDocument()
    expect(lineage).toHaveBeenCalledTimes(1)
    expect(lineage).toHaveBeenCalledWith('atlas', 'Atlas', 'accepted')
  })

  it('keeps an open draft when focus moves to another Sources field', async () => {
    vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockImplementation(function (this: HTMLElement) { return this.parentElement })
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host onSend={vi.fn()} />)
    fireEvent.click(screen.getByTestId('dashboard-edit-sources-button'))
    fireEvent.click(screen.getByTestId('dashboard-edit-source-accepted'))
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'keep me' } })
    fireEvent.focus(screen.getByTestId('dashboard-edit-source-verdict'))
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('keep me')
    expect(lineage).toHaveBeenCalledTimes(1)
  })

  it('drops an unpinned lineage on a wheel and asks the frame again after it scrolls', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    await screen.findByTestId('lineage-popover')
    const posted: { type?: string }[] = []
    vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m as { type?: string }) })
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { cb(0); return 0 })
    fireEvent.wheel(screen.getByTestId('dashboard-edit-capture'), { deltaY: 120, clientX: 20, clientY: 20 })
    expect(screen.queryByTestId('lineage-popover')).toBeNull()
    expect(posted.map(m => m.type)).toEqual(['kirocrew-dashboard:wheel', 'kirocrew-dashboard:point'])
  })

  it('scrolls the page on a touch drag and does not read the drag as a tap', () => {
    renderWithProviders(<Host />)
    const posted: { type?: string; dy?: number }[] = []
    vi.spyOn(frameWindow(), 'postMessage').mockImplementation((m: unknown) => { posted.push(m as { type?: string; dy?: number }) })
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { cb(0); return 0 })
    const capture = screen.getByTestId('dashboard-edit-capture')
    fireEvent.pointerDown(capture, { pointerType: 'touch', pointerId: 7, clientX: 20, clientY: 200 })
    fireEvent.pointerMove(capture, { pointerType: 'touch', pointerId: 7, clientX: 20, clientY: 150 })
    fireEvent.pointerUp(capture, { pointerType: 'touch', pointerId: 7, clientX: 20, clientY: 150 })
    fireEvent.click(capture, { clientX: 20, clientY: 150 })
    const wheels = posted.filter(m => m.type === 'kirocrew-dashboard:wheel')
    expect(wheels).toHaveLength(1)
    expect(wheels[0].dy).toBe(50)
    // The only point asked is the fresh pick after the scroll, not a tap's.
    expect(posted.filter(m => m.type === 'kirocrew-dashboard:point')).toHaveLength(1)
  })

  it('says a failed lineage read through the shared error notice', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockRejectedValue(new Error('boom'))
    renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    const notice = await screen.findByTestId('lineage-trace-failed')
    // Nothing typed yet, so the notice may hand the error off to a chat.
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('keeps a draft when Escape is pressed while the Dashboard is kept hidden', async () => {
    vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    const { rerender } = renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    // The side panel keeps the visited Dashboard mounted but hidden behind Workspace.
    rerender(<Host hidden />)
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.keyDown(window, { key: 'Escape' })
    rerender(<Host />)
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('show CI state')
  })

  it('offers no hand-off from a failed refresh while a request is being typed', async () => {
    const lineage = vi.spyOn(api, 'memberDashboardLineage').mockResolvedValue(LINEAGE)
    const { queryClient } = renderWithProviders(<Host />)
    pick({ path: 'accepted' })
    fireEvent.click(await screen.findByTestId('lineage-edit-open'))
    fireEvent.click(screen.getByTestId('lineage-ask'))
    fireEvent.change(screen.getByTestId('dashboard-edit-comment'), { target: { value: 'show CI state' } })
    // A field push refetches the lineage and the refetch fails; the last lineage and
    // the draft stay on screen beside the notice.
    lineage.mockRejectedValue(new Error('boom'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas', 'Atlas', 'lineage', 'accepted'] }) })
    const notice = await screen.findByTestId('lineage-trace-failed')
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.getByTestId('dashboard-edit-comment')).toHaveValue('show CI state')
  })
})
