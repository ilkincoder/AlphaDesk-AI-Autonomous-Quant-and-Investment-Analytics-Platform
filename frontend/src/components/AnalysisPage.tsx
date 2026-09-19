/** The Analysis page: ask a question, watch it being answered, read what it rests on.
 *
 * Four rules shape this file, and each is a way a chat UI gets its reader into trouble.
 *
 * **The server owns the conversation.** Messages come from `/analysis/conversations/{id}`, not
 * from a list kept here. The client shows a question immediately so it does not feel broken,
 * and then *reconciles* -- matched on `request_id` -- so a reply can never be counted twice.
 *
 * **One `request_id` per question.** It is generated once and persisted before the request is
 * sent. An uncertain outcome is resolved by asking the server about that same id, and a retry
 * sends that same request again. A fresh id after a timeout would quietly turn one question
 * into two paid analyses, which is the single most expensive mistake this page could make.
 *
 * **A timeout is not a failure.** The run is bounded by its own budget and finishes and
 * persists whatever the browser does. So the page says "Checking request status…" and looks,
 * rather than offering to ask again.
 *
 * **Answers are fenced by conversation.** A response whose conversation is no longer the open
 * one is discarded, so an answer to a question in the previous conversation cannot appear under
 * the current one.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '../api'
import {
  type ConversationHistory as ConversationHistoryResponse,
  type ProgressEvent,
  type Turn,
  createConversation,
  fetchConversation,
  streamMessage,
} from '../analysisApi'
import {
  type StoredConversation,
  activeConversationId,
  listConversations,
  pendingSubmission,
  rememberConversation,
  setActiveConversationId,
  setPendingSubmission,
  titleFor,
} from '../conversations'
import { Button } from './Button'
import { ChatComposer } from './ChatComposer'
import { ChatTranscript } from './ChatTranscript'
import { ConversationHistory } from './ConversationHistory'
import { Card } from './Card'
import { ErrorNotice } from './States'

/** How many turns a page of history asks for. */
const HISTORY_PAGE = 20

/** Polling for a `202`: every 3 seconds, at most 40 times -- about two minutes, which is the
 *  run's own budget plus a margin. Bounded on purpose: an unbounded poll is a page that keeps
 *  talking to the server for ever after something has gone wrong. */
const POLL_INTERVAL_MS = 3000
const POLL_MAX_ATTEMPTS = 40

/** A turn that will not change again. Anything else is still moving. */
const TERMINAL_STATUSES = new Set([
  'completed',
  'clarification_needed',
  'company_not_stored',
  'unsupported_capability',
  'invalid_citations',
  'budget_exhausted',
  'provider_failed',
  'service_failed',
  'configuration_error',
  'interrupted',
])

function isTerminal(turn: Turn): boolean {
  return TERMINAL_STATUSES.has(turn.status)
}

function newRequestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** A progress event, as one line a person can read. */
function describeEvent(event: ProgressEvent): string | null {
  switch (event.event) {
    case 'routing':
      return event.clarification_needed
        ? 'The request needs one more detail.'
        : `Reading the request${event.symbol ? ` for ${event.symbol}` : ''}…`
    case 'tool':
      return event.status
        ? `Ran ${event.tool} — ${event.status}${event.reason ? ` (${event.reason})` : ''}`
        : `Ran ${event.tool}`
    case 'findings':
      return 'Summarising what the tools returned…'
    case 'composing':
      return 'Writing the answer…'
    default:
      return null
  }
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}

type LoadState =
  | { phase: 'loading' }
  | { phase: 'ready' }
  | { phase: 'error'; message: string; missing: boolean }

/** What to show when a submission did not come back cleanly.
 *
 * `canRetrySame` is the important one. A timeout or a dropped connection does **not** prove the
 * analysis failed -- it is still running, bounded by its own budget, and will persist. So the
 * first offer is always to send *that same request* again, which the backend treats as the
 * duplicate it is. A fresh id is a second, explicit choice, and only ever after the server has
 * confirmed the first attempt is over.
 */
type Recovery = {
  message: string
  canRetrySame: boolean
  canRetryNew: boolean
}

export function AnalysisPage() {
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [load, setLoad] = useState<LoadState>({ phase: 'loading' })
  const [historyOpen, setHistoryOpen] = useState(false)
  const [conversations, setConversations] = useState<StoredConversation[]>([])
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState<{ requestId: string; message: string } | null>(null)
  const [progress, setProgress] = useState<string[]>([])
  const [statusNote, setStatusNote] = useState<string | null>(null)
  const [showCheckStatus, setShowCheckStatus] = useState(false)
  const [recovery, setRecovery] = useState<Recovery | null>(null)
  /** The offset of the *oldest* turn currently on screen. Zero means the beginning is here. */
  const [oldestOffset, setOldestOffset] = useState(0)

  // The conversation the page is *on*, readable from inside async work that started earlier.
  // Every response is checked against this before it is allowed to set state.
  const activeIdRef = useRef<string | null>(null)
  const pollTimer = useRef<number | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  const applyHistory = useCallback(
    (history: ConversationHistoryResponse, options: { append?: boolean } = {}) => {
      if (history.conversation_id !== activeIdRef.current) return
      setTurns((current) => {
        if (!options.append) return history.turns
        // Older turns go in front, de-duplicated by turn id: paging twice must not double
        // anything, and the order the server gave is the order they stay in.
        const known = new Set(current.map((turn) => turn.turn_id))
        const older = history.turns.filter((turn) => !known.has(turn.turn_id))
        return [...older, ...current]
      })
      setLoad({ phase: 'ready' })
    },
    [],
  )

  const loadConversation = useCallback(
    async (id: string) => {
      try {
        const first = await fetchConversation(id, { limit: HISTORY_PAGE, offset: 0 })
        if (id !== activeIdRef.current) return

        // The API pages oldest-first, so offset 0 is the *beginning* of a conversation. One
        // longer than a page would therefore open on its oldest turns and the answer just
        // given would be missing from the screen. The newest page is fetched explicitly once
        // the total is known -- a second request only when there is more than one page.
        if (first.total_turns > HISTORY_PAGE) {
          const offset = first.total_turns - HISTORY_PAGE
          const newest = await fetchConversation(id, { limit: HISTORY_PAGE, offset })
          if (id !== activeIdRef.current) return
          setOldestOffset(offset)
          applyHistory(newest)
          return
        }

        setOldestOffset(0)
        applyHistory(first)
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        if (id !== activeIdRef.current) return
        setLoad({
          phase: 'error',
          message: messageOf(error),
          missing: error instanceof ApiError && error.status === 404,
        })
      }
    },
    [applyHistory],
  )

  const loadEarlier = useCallback(async () => {
    const id = activeIdRef.current
    if (!id) return
    const offset = Math.max(0, oldestOffset - HISTORY_PAGE)
    try {
      const page = await fetchConversation(id, { limit: HISTORY_PAGE, offset })
      if (id !== activeIdRef.current) return
      setOldestOffset(offset)
      applyHistory(page, { append: true })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      if (id !== activeIdRef.current) return
      setRecovery({
        message: `Could not load earlier messages: ${messageOf(error)}`,
        canRetrySame: false,
        canRetryNew: false,
      })
    }
  }, [applyHistory, oldestOffset])

  const startConversation = useCallback(async () => {
    stopPolling()
    abortRef.current?.abort()
    activeIdRef.current = null
    setTurns([])
    setPending(null)
    setProgress([])
    setStatusNote(null)
    setShowCheckStatus(false)
    setRecovery(null)
    setOldestOffset(0)
    setLoad({ phase: 'loading' })
    setPendingSubmission(null)

    try {
      const created = await createConversation()
      activeIdRef.current = created.conversation_id
      setConversationId(created.conversation_id)
      setConversations(rememberConversation(created.conversation_id, 'New conversation'))
      setLoad({ phase: 'ready' })
    } catch (error) {
      setLoad({ phase: 'error', message: messageOf(error), missing: false })
    }
  }, [stopPolling])

  const openConversation = useCallback(
    async (id: string) => {
      stopPolling()
      abortRef.current?.abort()
      activeIdRef.current = id
      setConversationId(id)
      setActiveConversationId(id)
      setTurns([])
      setPending(null)
      setProgress([])
      setRecovery(null)
      setOldestOffset(0)
      setLoad({ phase: 'loading' })
      await loadConversation(id)
    },
    [loadConversation, stopPolling],
  )

  /** Poll until the turn stops moving, the page moves on, or the bound is reached. */
  const pollForTurn = useCallback(
    (id: string, requestId: string, attempt = 0) => {
      if (id !== activeIdRef.current) return
      if (attempt >= POLL_MAX_ATTEMPTS) {
        setStatusNote(
          'This is taking longer than expected. The analysis may still be running on the ' +
            'server — check again rather than asking a new question.',
        )
        setShowCheckStatus(true)
        return
      }

      pollTimer.current = window.setTimeout(async () => {
        if (id !== activeIdRef.current) return
        try {
          const history = await fetchConversation(id, { limit: HISTORY_PAGE })
          if (id !== activeIdRef.current) return
          const turn = history.turns.find((item) => item.request_id === requestId)
          if (turn && isTerminal(turn)) {
            setPending(null)
            setPendingSubmission(null)
            setProgress([])
            setStatusNote(null)
            setShowCheckStatus(false)
            applyHistory(history)
            return
          }
          pollForTurn(id, requestId, attempt + 1)
        } catch {
          // A failed poll is not a failed analysis. Keep trying until the bound.
          pollForTurn(id, requestId, attempt + 1)
        }
      }, POLL_INTERVAL_MS)
    },
    [applyHistory],
  )

  const submit = useCallback(
    async (message: string, requestId: string) => {
      const id = activeIdRef.current
      if (!id) return

      setPending({ requestId, message })
      setProgress([])
      setStatusNote(null)
      setShowCheckStatus(false)
      setRecovery(null)
      setPendingSubmission({ conversationId: id, requestId, message })

      const controller = new AbortController()
      abortRef.current = controller

      try {
        const turn = await streamMessage(
          { conversation_id: id, request_id: requestId, message },
          (event) => {
            if (id !== activeIdRef.current) return
            const line = describeEvent(event)
            if (line) setProgress((current) => [...current, line])
          },
          controller.signal,
        )

        if (id !== activeIdRef.current) return

        setPending(null)
        setPendingSubmission(null)
        setProgress([])
        setConversations(rememberConversation(id, titleFor(message)))
        // The server's copy is the truth; fetch it rather than splicing the response in, so a
        // turn that arrived twice cannot be shown twice.
        await loadConversation(id)
        setDraft((current) => (current === message ? '' : current))
        void turn
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        if (id !== activeIdRef.current) return

        // The draft comes back, because the question was never answered.
        setDraft(message)

        if (error instanceof ApiError && error.status === 409) {
          setPending(null)
          setPendingSubmission(null)
          setRecovery({
            message: error.message,
            // This id is spent: it was used for something else. Only a new one can be sent.
            canRetrySame: false,
            canRetryNew: false,
          })
          await loadConversation(id)
          return
        }
        if (error instanceof ApiError && error.status === 422) {
          setPending(null)
          setPendingSubmission(null)
          setRecovery({
            message: `That question was not accepted: ${error.message}`,
            canRetrySame: false,
            canRetryNew: false,
          })
          return
        }
        if (error instanceof ApiError && error.status === 404) {
          setPending(null)
          setPendingSubmission(null)
          setLoad({ phase: 'error', message: error.message, missing: true })
          return
        }
        if (error instanceof ApiError && error.status === 202) {
          // Still running. Ask the server rather than asking again.
          setStatusNote('Still running on the server.')
          pollForTurn(id, requestId)
          return
        }

        // Anything else -- a timeout, a dropped connection, a 503 -- is *unknown*, not failed.
        // The request id stays in storage, so the first thing offered is the same request
        // again; a new id is a second, explicit choice.
        setRecovery({
          message:
            'The request did not come back. It may still be running on the server — an ' +
            'analysis is not cancelled by a browser giving up. Check its status, or send ' +
            'the same request again.',
          canRetrySame: true,
          canRetryNew: true,
        })
        setShowCheckStatus(true)
      }
    },
    [loadConversation, pollForTurn],
  )

  const checkStatus = useCallback(async () => {
    const id = activeIdRef.current
    const outstanding = pendingSubmission()
    if (!id) return
    setStatusNote('Checking request status…')
    setShowCheckStatus(false)
    try {
      const history = await fetchConversation(id, { limit: HISTORY_PAGE })
      if (id !== activeIdRef.current) return
      const turn = outstanding
        ? history.turns.find((item) => item.request_id === outstanding.requestId)
        : undefined

      if (turn && isTerminal(turn)) {
        setPending(null)
        setPendingSubmission(null)
        setProgress([])
        setStatusNote(null)
        applyHistory(history)
        return
      }
      if (outstanding) {
        setStatusNote('Still running on the server.')
        pollForTurn(id, outstanding.requestId)
        return
      }
      applyHistory(history)
      setStatusNote(null)
    } catch (error) {
      setStatusNote(`Could not check: ${messageOf(error)}`)
      setShowCheckStatus(true)
    }
  }, [applyHistory, pollForTurn])

  const retrySameRequest = useCallback(() => {
    const outstanding = pendingSubmission()
    if (!outstanding) return
    void submit(outstanding.message, outstanding.requestId)
  }, [submit])

  const retryAsNewRequest = useCallback(() => {
    const outstanding = pendingSubmission()
    if (!outstanding) return
    // A *deliberate* new attempt, and only after the server confirmed the last one is over.
    void submit(outstanding.message, newRequestId())
  }, [submit])

  // Mount: restore, or start one. StrictMode runs effects twice in development, so the
  // in-flight guard is what stops two conversations being created on one page load.
  const booted = useRef(false)
  useEffect(() => {
    if (booted.current) return
    booted.current = true

    const stored = listConversations()
    setConversations(stored)

    const outstanding = pendingSubmission()
    const id = activeConversationId()
    if (id) {
      void openConversation(id).then(() => {
        if (outstanding && outstanding.conversationId === id) {
          // A submission was in flight when the page went away. Its outcome is unknown, so
          // ask rather than assume -- and never send a second one with a fresh id.
          void checkStatus()
        }
      })
      return
    }
    void startConversation()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Stop polling when the page goes away, and abort anything still streaming.
  useEffect(() => {
    return () => {
      stopPolling()
      abortRef.current?.abort()
    }
  }, [stopPolling])

  const busy = pending !== null
  const outstanding = pendingSubmission()

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-6 py-4">
        <div className="min-w-0">
          <h1 className="text-page font-bold text-ink">Analysis</h1>
          <p className="mt-0.5 max-w-[70ch] text-note text-dim">
            Ask about company filings, insider activity, market data, and your demo portfolio.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={() => setHistoryOpen((open) => !open)} aria-expanded={historyOpen}>
            {historyOpen ? 'Hide history' : 'History'}
          </Button>
          <Button variant="primary" onClick={() => void startConversation()} disabled={busy}>
            New conversation
          </Button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1 flex-col min-[900px]:flex-row">
        <ConversationHistory
          open={historyOpen}
          onClose={() => setHistoryOpen(false)}
          conversations={conversations}
          activeId={conversationId}
          onSelect={(id) => void openConversation(id)}
          onNew={() => void startConversation()}
          busy={busy}
        />

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {load.phase === 'loading' && turns.length === 0 ? (
            <div className="p-6">
              {/* Deliberately not the portfolio's skeleton: it stands in for cards that are
                  not coming, and a chat page's wait looks nothing like a table loading. */}
              <Card>
                <p role="status" className="text-body text-dim">
                  Loading the conversation…
                </p>
              </Card>
            </div>
          ) : load.phase === 'error' ? (
            <div className="p-6">
              <ErrorNotice
                title={load.missing ? 'That conversation is not on the server' : 'Could not load'}
                message={
                  load.missing
                    ? `${load.message} It may have been created by a different database, or removed. Start a new conversation to continue.`
                    : load.message
                }
                onRetry={() => void startConversation()}
              />
            </div>
          ) : (
            <>
              {oldestOffset > 0 && (
                <div className="border-b border-line px-6 py-2">
                  <Button onClick={() => void loadEarlier()}>Load earlier messages</Button>
                </div>
              )}

              {recovery && (
                <div className="px-6 pt-4">
                  <Card>
                    <div role="alert" className="border-l-2 border-l-danger pl-3">
                      <p className="text-note text-dim">{recovery.message}</p>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {recovery.canRetrySame && outstanding && (
                        <Button onClick={retrySameRequest}>Send that request again</Button>
                      )}
                      {recovery.canRetryNew && outstanding && (
                        <Button onClick={retryAsNewRequest}>Ask as a new request</Button>
                      )}
                      <Button onClick={() => void checkStatus()}>Check request status</Button>
                    </div>
                  </Card>
                </div>
              )}

              <ChatTranscript
                turns={turns}
                pendingQuestion={pending?.message ?? null}
                busy={busy}
                progress={progress}
                statusNote={statusNote}
                onCheckStatus={() => void checkStatus()}
                showCheckStatus={showCheckStatus}
              />

              <ChatComposer
                draft={draft}
                onDraftChange={setDraft}
                onSend={() => {
                  const message = draft.trim()
                  if (message.length === 0) return
                  void submit(message, newRequestId())
                }}
                disabled={busy || load.phase !== 'ready'}
                busy={busy}
              />
            </>
          )}
        </div>
      </div>
    </div>
  )
}
