import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, ExternalLink, KeyRound, Loader2, XCircle } from 'lucide-react'
import { api, type MuseLoginStatus } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { CopyCommand } from '../../components/agentHarness/CopyCommand'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

const STATUS_QUERY_KEY = ['muse-login'] as const
const POLL_MS = 3000

type Phase = 'idle' | 'starting' | 'pending' | 'ready' | 'failed'

/**
 * Settings > Agent Harness card for signing the gateway host in to a Muse
 * subscription -- the tool that replaces the SSH session for this step.
 *
 * `muse login` is a device flow: the gateway prints a code plus a Meta URL
 * and waits. This card starts that flow (`POST /api/muse-login/start`),
 * shows the code and link, polls (`GET /api/muse-login/status`) until the
 * owner approves in their own browser, and offers cancel. On `ready` it
 * reports back through `onConnected` so the parent re-takes the muse row's
 * verdict -- the card itself never claims the harness is usable.
 *
 * Rendered by `AgentBackendTab` inside the Muse row's detail, beside the
 * Kiro and Strands cards, in the same `ErrorBoundary` isolation: a throwing
 * card must not take the switch down with it. The credential file is never
 * read or displayed -- code and URL only, and only while pending.
 */
export function MuseLoginCard({ onConnected }: { onConnected?: () => void }) {
  const queryClient = useQueryClient()
  const [phase, setPhase] = useState<Phase>('idle')
  const [failure, setFailure] = useState('')
  // The grant, seeded from the start answer: the code is on screen without
  // waiting for the first status poll. Status only folds the verdict.
  const [grant, setGrant] = useState({ code: '', url: '' })
  const connected = useRef(onConnected)
  connected.current = onConnected

  const status = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: api.museLoginStatus,
    // Idle cards fetch nothing: the first read happens on Connect, so a
    // fresh card never flashes a status error. While the card is pending it
    // polls unconditionally -- the verdict, not the last answer, stops it --
    // and a settled card stops with it (`enabled`).
    enabled: phase === 'starting' || phase === 'pending',
    refetchInterval: POLL_MS,
    retry: false,
  })

  const start = useMutation({
    mutationFn: api.museLoginStart,
    onSuccess: (answer: MuseLoginStatus) => {
      setFailure('')
      setPhase(answer.state === 'pending' ? 'pending' : answer.state === 'ready' ? 'ready' : 'failed')
      if (answer.state === 'pending') setGrant({ code: answer.code, url: answer.url })
      else setGrant({ code: '', url: '' })
      if (answer.state !== 'pending' && answer.state !== 'ready') setFailure(answer.detail)
      void queryClient.invalidateQueries({ queryKey: STATUS_QUERY_KEY })
    },
    onError: (err) => {
      setFailure(err instanceof Error ? err.message : String(err))
      setPhase('failed')
    },
  })

  const cancel = useMutation({
    mutationFn: api.museLoginCancel,
    onSuccess: () => {
      setPhase('idle')
      setGrant({ code: '', url: '' })
      void queryClient.invalidateQueries({ queryKey: STATUS_QUERY_KEY })
    },
  })

  // The approval lands server-side while polling: fold the verdict in once.
  useEffect(() => {
    const state = status.data?.state
    if (state === undefined || state === 'pending' || state === 'none') return
    if (state === 'ready') {
      setPhase('ready')
      connected.current?.()
    } else if (state === 'failed' || state === 'expired') {
      setPhase('failed')
      setFailure(status.data?.detail ?? '')
    } else {
      setPhase('idle')
    }
  }, [status.data])

  const code = phase === 'pending' ? grant.code : ''
  const url = phase === 'pending' ? grant.url : ''

  return (
    <details
      className="border-t border-border pt-3"
      data-testid="muse-login-card"
      open={phase !== 'ready'}
    >
      <summary className="cursor-pointer text-[13px] text-text-strong focus-ring">
        <h3 className="m-0 inline text-[13px] font-semibold">
          <KeyRound className="lucide-inline mr-1.5" aria-hidden="true" />
          {i18nT('pages.developer.museLoginCard.title')}
        </h3>
      </summary>
      <div className="mt-2 space-y-3">
        {phase === 'idle' && (
          <>
            <p className="text-[12px] leading-relaxed text-muted">
              {i18nT('pages.developer.museLoginCard.connect_body')}
            </p>
            <Btn
              type="button"
              primary
              disabled={start.isPending}
              onClick={() => {
                setPhase('starting')
                start.mutate()
              }}
              data-testid="muse-login-connect"
            >
              {start.isPending ? (
                <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
              ) : (
                <KeyRound className="lucide-inline" aria-hidden="true" />
              )}
              {i18nT('pages.developer.museLoginCard.connect')}
            </Btn>
          </>
        )}
        {phase === 'starting' && !code && (
          <p className="flex items-center gap-2 text-[13px] text-muted" data-testid="muse-login-pending">
            <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
            {i18nT('pages.developer.museLoginCard.starting')}
          </p>
        )}
        {(phase === 'pending' || (phase === 'starting' && code)) && code && (
          <>
            <p className="text-[13px] font-medium text-text">
              {i18nT('pages.developer.museLoginCard.approve_title')}
            </p>
            <p className="text-[12px] leading-relaxed text-muted">
              {i18nT('pages.developer.museLoginCard.approve_body')}
            </p>
            <CopyCommand>
              <code>{code}</code>
            </CopyCommand>
            <div className="flex flex-wrap items-center gap-2">
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-[13px] font-medium underline-offset-2 hover:underline focus-ring"
                data-testid="muse-login-open"
              >
                {i18nT('pages.developer.museLoginCard.open_meta_sign_in')}
                <ExternalLink className="lucide-inline" aria-hidden="true" />
              </a>
              <Btn
                type="button"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate()}
                data-testid="muse-login-cancel"
              >
                {i18nT('pages.developer.museLoginCard.cancel')}
              </Btn>
            </div>
            <p className="flex items-center gap-2 text-[12px] text-muted" role="status">
              <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
              {i18nT('pages.developer.museLoginCard.waiting_approval')}
            </p>
          </>
        )}
        {phase === 'ready' && (
          <div className="flex items-start gap-2" data-testid="muse-login-ready">
            <CheckCircle2 className="lucide-inline mt-0.5 shrink-0 text-ok-text" aria-hidden="true" />
            <div>
              <p className="text-[13px] font-medium text-text">
                {i18nT('pages.developer.museLoginCard.signed_in')}
              </p>
              <p className="text-[12px] leading-relaxed text-muted">
                {i18nT('pages.developer.museLoginCard.signed_in_body')}
              </p>
            </div>
          </div>
        )}
        {phase === 'failed' && (
          <>
            <div className="flex items-start gap-2" data-testid="muse-login-failed">
              <XCircle className="lucide-inline mt-0.5 shrink-0 text-warn-text" aria-hidden="true" />
              <p className="text-[13px] font-medium text-text">
                {i18nT('pages.developer.museLoginCard.sign_in_failed')}
              </p>
            </div>
            <Btn
              type="button"
              onClick={() => {
                setPhase('idle')
                setFailure('')
                setGrant({ code: '', url: '' })
              }}
              data-testid="muse-login-retry"
            >
              {i18nT('pages.developer.museLoginCard.try_again')}
            </Btn>
          </>
        )}
        <ErrorNotice
          className="mt-1"
          message={
            phase === 'failed' && failure
              ? failure
              : start.error
                ? start.error instanceof Error
                  ? start.error.message
                  : String(start.error)
                : status.error
                  ? status.error instanceof Error
                    ? status.error.message
                    : String(status.error)
                  : null
          }
          testId="muse-login-error"
        />
      </div>
    </details>
  )
}
