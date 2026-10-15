/** News & Trading: what has been published about the holdings, and the rebalance proposal.
 *
 * Everything here is *read* except two buttons. "Refresh news" reaches the news providers and
 * "Rebalance proposal" runs the proposal workflow — and both are bounded on the server, so
 * pressing either costs a predictable amount. Nothing runs when the page opens: a page that
 * ingested on load would spend a provider's requests on whoever happened to click the sidebar.
 * The one thing the page does read on open is the *stored* proposal, which costs nothing and is
 * what makes a reload restore what was there.
 *
 * **The proposal is portfolio-wide, and the news filters are not.** The two sit on one page
 * because the evidence behind a proposal is news, but the filters above narrow a *listing* and
 * have nothing to do with which portfolio is analysed. A filter that quietly restricted the
 * analysis would produce a proposal about a different portfolio than the one on screen, so the
 * request carries no filter at all — it is a request for this portfolio, and there is exactly
 * one.
 *
 * **Two times are shown and they are not the same fact.** An article's publication time is
 * the publisher's, and the last successful ingestion time is ours. Showing one where the
 * other belongs is how a week-old release comes to look like breaking news, so they are
 * labelled separately and neither is inferred from the other.
 *
 * **Nothing here calls the feed real-time.** It is delayed, and the page says so — with the
 * backend's own sentence, which is returned as `timeliness` so the two cannot drift apart.
 */

import { type FormEvent, useCallback, useEffect, useRef, useState } from 'react'

import {
  decideProposal,
  fetchBatch,
  fetchNews,
  fetchProposal,
  ingestNews,
  searchNews,
  streamProposal,
} from '../api'
import type {
  ExecutionBatch,
  NewsArticle,
  NewsIngest,
  NewsList,
  NewsSearch,
  RebalanceProposal,
} from '../api'
import { formatDateTime, formatSyncTime } from '../format'
import { batchColour, batchLabel, intentLabel, isSettled, POLL_INTERVAL_MS } from '../orders'
import { Button } from './Button'
import { Card } from './Card'
import { MarketPulse, PULSE_FADE_MS } from './MarketPulse'
import { RebalanceProposal as ProposalSection } from './RebalanceProposal'
import { Banner, ErrorNotice } from './States'

const PAGE_SIZE = 20

const CATEGORIES = [
  { value: '', label: 'All categories' },
  { value: 'company', label: 'Company news' },
  { value: 'macro', label: 'Economy & policy' },
]

/** The labels a reader sees for the source slugs the backend stores. */
const SOURCE_LABELS: Record<string, string> = {
  alpaca_news: 'Alpaca (Benzinga)',
  official_fed_monetary: 'Federal Reserve',
  official_bls_cpi: 'BLS — CPI',
  official_bls_employment_situation: 'BLS — Employment',
}

const SOURCES = [
  { value: '', label: 'All sources' },
  ...Object.entries(SOURCE_LABELS).map(([value, label]) => ({ value, label })),
]

function labelFor(slug: string): string {
  return SOURCE_LABELS[slug] ?? slug
}

/** One state at a time, so "refreshing and also showing freshness" is unrepresentable.
 *
 * `stale` is the important one: a refresh failed but what was already on screen is kept
 * and labelled, rather than cleared or passed off as current.
 */
type ListState =
  | { phase: 'loading' }
  | { phase: 'ready'; list: NewsList }
  | { phase: 'stale'; list: NewsList; message: string }
  | { phase: 'error'; message: string }

type SearchState =
  | { phase: 'idle' }
  | { phase: 'searching' }
  | { phase: 'ready'; result: NewsSearch }
  | { phase: 'error'; message: string }

/** The proposal, as the page holds it.
 *
 * `restoring` is distinct from `idle` only so the page does not briefly claim there is no
 * proposal while it is still asking whether there is one. Nothing is shown for either.
 */
type ProposalState =
  | { phase: 'restoring' }
  | { phase: 'idle' }
  | { phase: 'generating'; stage: string | null }
  | { phase: 'ready'; proposal: RebalanceProposal }
  | { phase: 'error'; message: string }

/** The batch an approval opened: while it is being placed, and what it turned out to be.
 *
 * Approving does **not** place the orders. It records the decision and opens a batch, and a
 * worker on the server submits it on its own thread, on its own schedule -- so `deciding` and
 * `watching` are two different waits and not one: the first is this browser asking the server to
 * record something, the second is a worker this page does not control doing something it cannot
 * see. One spinner over both would claim the page is placing orders, which it never does.
 *
 * `settled` and `gave_up` are kept after the flow closes, because they are what the notice above
 * the news is written from.
 */
type PlacingState =
  | { phase: 'idle' }
  /** The decision request is in flight, so there is no batch to watch yet. */
  | { phase: 'deciding' }
  | { phase: 'watching'; batchId: string; batch: ExecutionBatch | null }
  | { phase: 'settled'; batch: ExecutionBatch }
  /** The wait ended before the batch did. `batch` is what was last seen, if anything. */
  | { phase: 'gave_up'; batch: ExecutionBatch | null }
  | { phase: 'error'; message: string }

/** How long to follow a batch before giving up on it.
 *
 * Not a limit on the execution -- the worker finishes the batch whether or not anybody is
 * watching. It is a limit on how long this page holds the screen: a batch whose orders sit
 * accepted because the market is closed may not settle for hours, and a loading screen that
 * waits for one would trap the reader rather than inform them.
 *
 * It is above `UNCERTAIN_GRACE_SECONDS` (60s, `app/order_executor`) on purpose. A submission the
 * broker did not answer is not recorded as `not_found`, and does not halt the batch, until that
 * grace has passed and two reconciliations have missed it -- so a ceiling under it would give up
 * on a batch that was about to reach an answer.
 */
export const WAIT_CEILING_MS = 90_000

/** What each stage of the run is, in a reader's words.
 *
 * The names are the backend's own (`app/agent/module2`), and a stage that is not in this map is
 * shown by its identifier rather than hidden -- an unrecognised stage is a stage this page has
 * not been taught to describe, which is worth seeing rather than swallowing.
 */
const STAGE_LABELS: Record<string, string> = {
  syncing: 'Synchronising the portfolio',
  retrieving_news: 'Reading the relevant news',
  proposing_targets: 'Proposing target weights',
  calculating: 'Calculating the trades',
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}

/** How long a wait has to last before it is worth covering the screen for.
 *
 * Rejecting is one database write with no broker call and comes back in tens of milliseconds.
 * A screen that covers the whole application for that long is a flash, and a flash reads as a
 * fault rather than as progress -- so a short wait leaves the page as it is, and only a wait
 * long enough to be a wait gets the screen.
 */
export const WORK_SCREEN_DELAY_MS = 300

/** Whether the work screen should be up, and whether it is fading out.
 *
 * Two flags rather than one: the screen has to stay mounted through its fade, and unmounting it
 * the moment the work ends would cut the transition off on its first frame. `show` is whether it
 * is here at all, `leaving` is how it looks on the way out.
 */
function useWorkScreen(active: boolean): { show: boolean; leaving: boolean } {
  const [show, setShow] = useState(false)
  const [leaving, setLeaving] = useState(false)

  useEffect(() => {
    if (active) {
      setLeaving(false)
      const timer = window.setTimeout(() => setShow(true), WORK_SCREEN_DELAY_MS)
      return () => window.clearTimeout(timer)
    }

    // Read from the render this effect is running in, which is the one where the work ended.
    // Listing `show` as a dependency would re-run this on the state it sets.
    if (!show) return
    setLeaving(true)
    const timer = window.setTimeout(() => {
      setShow(false)
      setLeaving(false)
    }, PULSE_FADE_MS)
    return () => window.clearTimeout(timer)
  }, [active])

  return { show, leaving }
}

export function NewsPage({
  active = true,
  onOpenOrders,
}: {
  active?: boolean
  /** Called with the batch an approval opened, so the shell can show the Orders page. */
  onOpenOrders?: (batchId: string) => void
}) {
  const [state, setState] = useState<ListState>({ phase: 'loading' })
  const [search, setSearch] = useState<SearchState>({ phase: 'idle' })
  const [query, setQuery] = useState('')
  const [provider, setProvider] = useState('')
  const [category, setCategory] = useState('')
  // What the last ingestion did. Kept apart from the listing because it is a fact about
  // the run rather than about the articles, and it has to survive the listing being read
  // again underneath it -- which it now is, by the effect below.
  const [notice, setNotice] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  // Bumped when something that finished later wants the listing read again. The effect
  // below owns the reading, so the reload uses the selection as it is *now* rather than
  // the one the finished request was made with.
  const [reloads, setReloads] = useState(0)

  // The listing request in flight, if any. Starting one cancels the one before it, because
  // the newest selection is the question being asked: a filter changed while a request was
  // running would otherwise be dropped, leaving the page showing the previous selection's
  // articles under the new selection's controls. This is also what makes StrictMode's
  // second run in development safe -- it supersedes the first rather than being refused by
  // it, which is what left the page loading for ever.
  const listing = useRef<AbortController | null>(null)
  // The ingestion, which is the one call here that reaches a provider. It is neither
  // cancelled nor started twice: pressing Refresh again will not read the feeds again.
  const ingesting = useRef(false)

  const [proposal, setProposal] = useState<ProposalState>({ phase: 'restoring' })
  // The proposal run. Neither cancelled nor started twice either: the button is disabled while
  // it works, and this ref is what makes that true even for a click that lands between the
  // press and the re-render. The server refuses a second run as well -- the guard here is what
  // stops the user being told about a race they did not need to see.
  const generating = useRef(false)
  // Dismissing hides the panel for this visit. It does not delete the stored proposal, and
  // nothing about it reaches a broker -- and it is deliberately not a rejection, which is a
  // decision recorded against the proposal rather than a fact about this browser.
  const [dismissed, setDismissed] = useState(false)
  // The decision request, if one is in flight. Neither button is pressable while it is: two
  // clicks would be two decisions asked for, and the second would be answered with a refusal
  // the user did not need to see.
  const [deciding, setDeciding] = useState(false)
  // What the server said about the last decision attempt, in its own words. A refusal's
  // sentence, or a renewal's, is the useful part and is shown rather than paraphrased.
  const [decisionNotice, setDecisionNotice] = useState<string | null>(null)

  /** Whether the proposal flow has the page rather than the news.
   *
   * Deliberately not derived from `proposal.phase`. A proposal restored from the store is `ready`
   * on load, and seizing the page for it every time somebody came here for news would be this
   * page answering a question nobody asked -- so entering the flow is something a person does,
   * and the restored one is offered as a notice instead. */
  const [flowOpen, setFlowOpen] = useState(false)

  // The approval and the batch it opened. See `PlacingState`.
  const [placing, setPlacing] = useState<PlacingState>({ phase: 'idle' })

  /** The batch being followed, as an identifier.
   *
   * Held apart from `placing` so the polling effect below depends on *which* batch this is and
   * not on the status it happens to be at. Depending on `placing` would tear the interval down
   * and build it again on every poll, which is a timer that never fires twice. */
  const watchingId = placing.phase === 'watching' ? placing.batchId : null

  /** The batch's progress, re-read until it stops.
   *
   * The first read is immediate rather than after one interval: the worker may already have
   * submitted, and waiting four seconds to find that out would be a delay this page invented.
   */
  useEffect(() => {
    if (!active || watchingId === null) return

    let stopped = false
    const deadline = Date.now() + WAIT_CEILING_MS

    const poll = async () => {
      let batch: ExecutionBatch | null = null
      try {
        batch = await fetchBatch(watchingId)
      } catch {
        // Reading the batch failed, which says nothing about the batch. Report what was last
        // seen and let the reader reach the Orders page rather than inventing a failure.
        if (!stopped) setPlacing({ phase: 'gave_up', batch: null })
        return
      }
      if (stopped) return

      if (batch !== null && isSettled(batch)) {
        setPlacing({ phase: 'settled', batch })
        return
      }
      if (batch === null || Date.now() >= deadline) {
        setPlacing({ phase: 'gave_up', batch })
        return
      }
      setPlacing({ phase: 'watching', batchId: watchingId, batch })
    }

    void poll()
    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [active, watchingId])

  // The flow ends when the batch does, not when the decision is recorded: the news is what the
  // reader came for, and holding the screen after the answer is known would be the page keeping
  // them from it. The outcome stays on screen as the notice below.
  useEffect(() => {
    if (placing.phase === 'settled' || placing.phase === 'gave_up') setFlowOpen(false)
  }, [placing.phase])

  const load = useCallback(
    async (filters = { provider, category }) => {
      listing.current?.abort()
      const controller = new AbortController()
      listing.current = controller
      setRefreshing(true)
      try {
        const list = await fetchNews(filters, { limit: PAGE_SIZE }, controller.signal)
        setState({ phase: 'ready', list })
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        const message = messageOf(error)
        setState((current) =>
          current.phase === 'ready' || current.phase === 'stale'
            ? { phase: 'stale', list: current.list, message }
            : { phase: 'error', message },
        )
      } finally {
        // Only the request that is still the current one reports the page idle: a
        // superseded request finishing late must not say so while its replacement runs.
        if (listing.current === controller) {
          listing.current = null
          setRefreshing(false)
        }
      }
    },
    [provider, category],
  )

  // `reloads` is not read: it is the signal that a request which finished later wants this
  // effect to read the listing again, with the selection as it is now.
  useEffect(() => {
    if (!active) return
    void load()
  }, [active, load, reloads])

  /** Read every source once, then read the listing back. The only call that reaches a
   *  provider. */
  const refresh = useCallback(async () => {
    if (ingesting.current) return
    ingesting.current = true
    setRefreshing(true)
    try {
      const result = await ingestNews()
      setNotice(describeIngest(result))
      setSearch({ phase: 'idle' })
      // Asked for through the effect rather than fetched here. This callback was created by
      // the render that was on screen when the button was pressed, and a filter changed
      // during the run would not be in it -- so the page would come back showing the
      // selection from before the change.
      setReloads((count) => count + 1)
    } catch (error) {
      const message = messageOf(error)
      setNotice(null)
      setState((current) =>
        current.phase === 'ready' || current.phase === 'stale'
          ? { phase: 'stale', list: current.list, message }
          : { phase: 'error', message },
      )
    } finally {
      ingesting.current = false
      setRefreshing(false)
    }
  }, [])

  const runSearch = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const trimmed = query.trim()
      if (!trimmed) return
      setSearch({ phase: 'searching' })
      try {
        const result = await searchNews(trimmed, { provider, category })
        setSearch({ phase: 'ready', result })
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setSearch({ phase: 'error', message: messageOf(error) })
      }
    },
    [query, provider, category],
  )

  /** Read the stored proposal back, so a reload restores what was there.
   *
   * Deliberately not cancelled by a later filter change: the proposal has nothing to do with
   * the filters, and superseding this read when one changes would hide a proposal that is still
   * current. StrictMode's second run in development re-reads it, which is harmless -- the call
   * is a GET of one row.
   */
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    void (async () => {
      try {
        const view = await fetchProposal(controller.signal)
        setProposal(
          view.proposal ? { phase: 'ready', proposal: view.proposal } : { phase: 'idle' },
        )
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setProposal({ phase: 'error', message: messageOf(error) })
      }
    })()
    return () => controller.abort()
  }, [active])

  /** Record a decision about the proposal on screen.
   *
   * The body carries the decision and nothing else. What gets executed is resolved server-side
   * from the proposal's own stored calculation, so there is nothing here a browser could send
   * that would change the orders.
   *
   * A plan whose prices have moved comes back as `renewal_required` rather than as an approval:
   * the refreshed estimates are shown and a second press confirms them. That second press is
   * `confirm: true` -- the only thing it says is "yes, at these prices".
   */
  const decide = useCallback(
    async (decision: 'approve' | 'reject', confirm = false) => {
      if (deciding || proposal.phase !== 'ready') return
      const proposalId = proposal.proposal.proposal_id
      setDeciding(true)
      setDecisionNotice(null)
      // Approving is the one decision that leads somewhere to wait, so the screen changes on the
      // press rather than when the request comes back. A rejection has nothing to follow.
      if (decision === 'approve') setPlacing({ phase: 'deciding' })
      try {
        const outcome = await decideProposal(proposalId, {
          decision,
          idempotency_key: `decision-${proposalId}-${Date.now()}-${Math.random()
            .toString(36)
            .slice(2, 10)}`,
          confirm,
        })
        setDecisionNotice(outcome.detail)
        if (outcome.decision !== null) {
          setProposal({
            phase: 'ready',
            proposal: {
              ...proposal.proposal,
              decision: outcome.decision,
              batch_id: outcome.batch_id,
            },
          })
        }
        if (outcome.batch_id !== null) {
          // Keyed on the batch id rather than on the word `approved`: a second press that lands
          // on an approval already recorded comes back as `already_decided` *with* its batch, and
          // following that reports where the orders actually are instead of reporting nothing.
          // Every other outcome -- a refusal, a renewal, a rejection -- carries no batch, and
          // each is an answer the panel already shows.
          //
          // The orders are the server's to submit, whether or not anybody is looking, so this
          // page follows the batch rather than claiming to place it. `onOpenOrders` is left to
          // the report's own button: sending the reader away the moment they approve is how they
          // lose the news they were reading.
          setPlacing({ phase: 'watching', batchId: outcome.batch_id, batch: null })
        } else if (decision === 'approve') {
          setPlacing({ phase: 'idle' })
        }
      } catch (error) {
        setDecisionNotice(messageOf(error))
        if (decision === 'approve') setPlacing({ phase: 'error', message: messageOf(error) })
      } finally {
        setDeciding(false)
      }
    },
    [deciding, proposal],
  )

  /** Ask for a proposal, and say which stage the run is in while it works. */
  const generate = useCallback(async () => {
    if (generating.current) return
    generating.current = true
    setDismissed(false)
    setPlacing({ phase: 'idle' })
    setFlowOpen(true)
    setProposal({ phase: 'generating', stage: null })
    try {
      // A fresh identifier per press, so a previous request's stored outcome is never served
      // in place of a new run -- and so a retry after a failure is the user's decision rather
      // than something this page does on its own.
      const requestId = `proposal-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
      const generated = await streamProposal(requestId, (event) => {
        if (event.event === 'stage') {
          setProposal({ phase: 'generating', stage: event.stage ?? null })
        }
      })
      setProposal({ phase: 'ready', proposal: generated })
    } catch (error) {
      setProposal({ phase: 'error', message: messageOf(error) })
    } finally {
      generating.current = false
    }
  }, [])

  const loadMore = useCallback(async () => {
    if (state.phase !== 'ready') return
    // Through the same slot as a load, so a filter change arriving while the next page is
    // on its way supersedes it rather than appending a page of the previous selection.
    listing.current?.abort()
    const controller = new AbortController()
    listing.current = controller
    setLoadingMore(true)
    try {
      const next = await fetchNews(
        { provider, category },
        { limit: PAGE_SIZE, offset: state.list.articles.length },
        controller.signal,
      )
      // Appended to whatever is on screen when the page arrives, not to what was on screen
      // when it was asked for.
      setState((current) =>
        current.phase === 'ready'
          ? {
              phase: 'ready',
              list: { ...next, articles: [...current.list.articles, ...next.articles] },
            }
          : current,
      )
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setState((current) =>
        current.phase === 'ready'
          ? { phase: 'stale', list: current.list, message: messageOf(error) }
          : current,
      )
    } finally {
      if (listing.current === controller) {
        listing.current = null
        setLoadingMore(false)
      }
    }
  }, [state, provider, category])

  const searching = search.phase !== 'idle'
  // A placement is included, and not only a run: starting a second generation while a batch is
  // being submitted would replace the screen that is following it, and the outcome the reader is
  // waiting for would never be reported.
  const busy = proposal.phase === 'generating' || placing.phase === 'watching'

  // Everything the page can be waiting on. A decision is in here as well as a run: approving
  // reads the broker before it records anything, so it is a real wait -- and rejecting is not,
  // which is what the delay below is for.
  const workScreen = useWorkScreen(
    busy || placing.phase === 'deciding',
  )

  return (
    <div>
      <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div>
          <h1 className="text-title font-bold text-ink">News &amp; Trading</h1>
          <p className="mt-1 text-note text-dim">
            Coverage of your holdings, the economic releases that move them, and a rebalance
            proposal for the portfolio.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {/* Not "Live", and not "Real-time". The feed is delayed, and a badge is exactly
              where an unearned claim would be read as one. */}
          <span className="rounded-full bg-selected px-2.5 py-1 text-label text-accent-ink">
            Delayed
          </span>
          <Button
            onClick={() => {
              if (!refreshing) void refresh()
            }}
            aria-disabled={refreshing}
            className={refreshing ? 'opacity-50' : ''}
          >
            {refreshing ? 'Refreshing…' : 'Refresh news'}
          </Button>
          {/* The page's one primary action, and the only thing here that runs the agent. While
              the proposal has the page it becomes the way back to the news: the run is the
              server's and keeps going either way, so leaving is not a cancellation. */}
          {flowOpen ? (
            <Button onClick={() => setFlowOpen(false)}>Back to news</Button>
          ) : (
            <Button
              variant="primary"
              onClick={() => {
                if (!busy) void generate()
              }}
              // `disabled` rather than `aria-disabled`, unlike the buttons above: this one must
              // not be pressable twice, and the press is what the guard in `generate` also
              // covers for anything that slips through.
              disabled={busy}
            >
              {busy ? 'Generating…' : 'Rebalance proposal'}
            </Button>
          )}
        </div>
      </header>

      {/* Dimmed out of reach while the work screen is up: the page underneath is what is doing
          the work, and it stays rendered so the fade reveals it rather than a blank column --
          but a reader must not be able to tab into a page they cannot see. */}
      <div inert={workScreen.show}>
        {flowOpen ? (
          <ProposalFlow
            state={proposal}
            placing={placing}
            dismissed={dismissed}
            deciding={deciding}
            notice={decisionNotice}
            onDismiss={() => {
              setDismissed(true)
              setFlowOpen(false)
            }}
            onRetry={() => void generate()}
            onDecide={(decision, confirm) => void decide(decision, confirm)}
          />
        ) : (
        <>
          <OutcomeNotice
            placing={placing}
            onDismiss={() => setPlacing({ phase: 'idle' })}
            onOpenOrders={onOpenOrders}
          />
          <ReadyNotice
            proposal={proposal}
            dismissed={dismissed}
            onReview={() => setFlowOpen(true)}
          />

          <form onSubmit={runSearch} className="mt-4 flex flex-wrap items-end gap-3">
        <div className="min-w-[200px] flex-1">
          <label htmlFor="news-search" className="text-label text-faint">
            Search the stored news
          </label>
          <input
            id="news-search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="data centre spending"
            className="mt-1.5 h-9 w-full rounded-md border border-line bg-page px-3 text-body text-ink"
          />
        </div>

        <div>
          <label htmlFor="news-provider" className="text-label text-faint">
            Source
          </label>
          <select
            id="news-provider"
            value={provider}
            onChange={(event) => setProvider(event.target.value)}
            className="mt-1.5 h-9 rounded-md border border-line bg-page px-3 text-body text-ink"
          >
            {SOURCES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="news-category" className="text-label text-faint">
            Category
          </label>
          <select
            id="news-category"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
            className="mt-1.5 h-9 rounded-md border border-line bg-page px-3 text-body text-ink"
          >
            {CATEGORIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <Button type="submit" variant="primary" aria-disabled={search.phase === 'searching'}>
          {search.phase === 'searching' ? 'Searching…' : 'Search'}
        </Button>

        {/* Filter changes apply to the listing immediately, so the search has to be
            dismissible rather than replaced in place. */}
        {searching && (
          <Button type="button" onClick={() => setSearch({ phase: 'idle' })}>
            Back to latest
          </Button>
        )}
      </form>

      <div className="mt-6 space-y-4">
        {state.phase === 'stale' && (
          <Banner>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <span>Showing previously loaded articles. {state.message}</span>
              <Button className="bg-transparent" onClick={() => void load()}>
                Retry
              </Button>
            </div>
          </Banner>
        )}

        {notice !== null && (
          <Banner>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <span>{notice}</span>
              <Button className="bg-transparent" onClick={() => setNotice(null)}>
                Dismiss
              </Button>
            </div>
          </Banner>
        )}

        {state.phase === 'error' ? (
          <ErrorNotice
            title="Could not load the news."
            message={state.message}
            onRetry={() => void load()}
          />
        ) : searching ? (
          <SearchResults search={search} />
        ) : (
          <LatestArticles
            state={state}
            onLoadMore={() => void loadMore()}
            loadingMore={loadingMore}
            filtered={provider !== '' || category !== ''}
          />
        )}
      </div>
        </>
        )}
      </div>

      {/* The same screen the application opens on, for the same reason: something is being
          worked on and the page behind it is not what the reader should be reading. Full
          viewport, so the sidebar goes under it too -- during a run there is nothing on this
          page to do, and leaving half of it live would only invite a second run. */}
      {workScreen.show && (
        <MarketPulse message={workMessage(proposal, placing)} leaving={workScreen.leaving} />
      )}
    </div>
  )
}

/** What the work screen says it is waiting on.
 *
 * The run's own stage names where it has one, and the batch's own status where it does not --
 * so the line changes as the work moves rather than sitting on one word for a minute. A stage
 * the backend has added and this page has not been taught is shown by its identifier, the same
 * as `STAGE_LABELS` does.
 */
function workMessage(proposal: ProposalState, placing: PlacingState): string {
  if (placing.phase === 'deciding') return 'Recording the decision'
  if (placing.phase === 'watching') {
    const batch = placing.batch
    if (batch === null) return 'Placing orders'
    const sent = sentCount(batch)
    const total = batch.orders.length
    return `${batchLabel(batch)} · ${sent} of ${total} ${total === 1 ? 'order' : 'orders'} sent`
  }
  if (proposal.phase === 'generating') {
    return proposal.stage === null
      ? 'Starting the proposal'
      : (STAGE_LABELS[proposal.stage] ?? proposal.stage)
  }
  return 'Working'
}

/** The outcome of an approval, reported where the reader is.
 *
 * This is why approving does not send anybody to the Orders page. The batch is the server's and
 * its detail is one click away, but leaving the news the moment an approval lands is how the
 * reader loses the thing they were reading -- and the answer to "did my orders go through?" is a
 * sentence, not a page.
 *
 * A batch that was still working when the wait gave up says so, rather than being reported as one
 * that finished: the orders may since have filled, and only the Orders page knows.
 */
function OutcomeNotice({
  placing,
  onDismiss,
  onOpenOrders,
}: {
  placing: PlacingState
  onDismiss: () => void
  onOpenOrders?: (batchId: string) => void
}) {
  if (placing.phase !== 'settled' && placing.phase !== 'gave_up') return null
  const batch = placing.batch

  return (
    <div className="mt-4">
      <Banner>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span>
            {batch === null
              ? 'The batch was approved, but its progress could not be read here. Its orders are on the Orders page.'
              : summarise(batch, placing.phase === 'gave_up')}
          </span>
          {batch !== null && onOpenOrders !== undefined && (
            <Button className="bg-transparent" onClick={() => onOpenOrders(batch.batch_id)}>
              View orders
            </Button>
          )}
          <Button className="bg-transparent" onClick={onDismiss}>
            Dismiss
          </Button>
        </div>
      </Banner>
    </div>
  )
}

/** What a batch ended up doing, in one sentence.
 *
 * Counted from the orders the batch holds rather than from its status alone: `partial` and
 * `halted` say the batch stopped, and how far it got is the part a reader is asking about.
 */
function summarise(batch: ExecutionBatch, gaveUp: boolean): string {
  const counts = new Map<string, number>()
  for (const order of batch.orders) {
    const label = intentLabel(order)
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  const total = batch.orders.length
  const orders = `${total} ${total === 1 ? 'order' : 'orders'}`
  const breakdown = [...counts].map(([label, count]) => `${count} ${label.toLowerCase()}`).join(', ')

  if (gaveUp) {
    return `Approved — ${batchLabel(batch).toLowerCase()}. ${orders}: ${breakdown}. It is still being worked on; the Orders page has the latest.`
  }
  return `${batchLabel(batch)} — ${orders}: ${breakdown}.`
}

/** A proposal the page is holding but not showing.
 *
 * A stored proposal is restored on load and a run that finishes after the reader has gone back to
 * the news lands here too. Neither should take the page, and neither should be invisible: the
 * reader asked for a proposal, and this is where it is.
 */
function ReadyNotice({
  proposal,
  dismissed,
  onReview,
}: {
  proposal: ProposalState
  dismissed: boolean
  onReview: () => void
}) {
  if (proposal.phase !== 'ready' || dismissed) return null
  // A decided proposal is not waiting for anybody. Its outcome is on the Orders page, and
  // offering to review it again would be offering a decision that has already been made.
  if (proposal.proposal.decision !== null) return null

  return (
    <div className="mt-4">
      <Banner>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span>A rebalance proposal is ready to review. Nothing has been sent yet.</span>
          <Button className="bg-transparent" onClick={onReview}>
            Review
          </Button>
        </div>
      </Banner>
    </div>
  )
}

/** The proposal flow, on the page rather than under the news.
 *
 * One component for the three things the flow can be showing, because they are one screen
 * succeeding another: the run, the proposal, and the wait after approving. The news is not
 * rendered at all while this is up -- it is 178 cards and a Load more, and the reader cannot be
 * asked to scroll past them to see the thing they pressed a button for.
 */
function ProposalFlow({
  state,
  placing,
  dismissed,
  deciding,
  notice,
  onDismiss,
  onRetry,
  onDecide,
}: {
  state: ProposalState
  placing: PlacingState
  dismissed: boolean
  deciding: boolean
  notice: string | null
  onDismiss: () => void
  onRetry: () => void
  onDecide: (decision: 'approve' | 'reject', confirm: boolean) => void
}) {
  if (placing.phase === 'deciding' || placing.phase === 'watching') {
    return <PlacingPanel placing={placing} />
  }

  if (placing.phase === 'error') {
    return (
      <section className="mt-6 border-t border-line pt-6">
        <h2 className="text-heading font-semibold text-ink">Rebalance proposal</h2>
        <div className="mt-4">
          <ErrorNotice
            title="Could not approve the proposal."
            message={placing.message}
            onRetry={onDismiss}
          />
        </div>
      </section>
    )
  }

  return (
    <ProposalPanel
      state={state}
      dismissed={dismissed}
      onDismiss={onDismiss}
      onRetry={onRetry}
      deciding={deciding}
      notice={notice}
      onDecide={onDecide}
    />
  )
}

/** How many of a batch's orders have been to the broker.
 *
 * `planned` is an order the worker has not reached and `submitting` is one it is sending right
 * now. Everything else, including the refusals, is an answer that came back -- and counting a
 * planned order as sent would report a submission that never happened.
 */
function sentCount(batch: ExecutionBatch): number {
  return batch.orders.filter((order) => !['planned', 'submitting'].includes(order.status)).length
}

/** The wait after an approval: the decision being recorded, then the batch being placed.
 *
 * The two are named separately because they are separate waits. This page records a decision and
 * reads what a server-side worker does with it; saying "placing your orders" over the whole thing
 * would be the page claiming work that is not its own.
 */
function PlacingPanel({ placing }: { placing: PlacingState }) {
  const batch = placing.phase === 'watching' ? placing.batch : null
  // `planned` is an order the worker has not reached and `submitting` is one it is sending now.
  const sent = batch === null ? 0 : sentCount(batch)

  return (
    <section className="mt-6 border-t border-line pt-6">
      <h2 className="text-heading font-semibold text-ink">Placing orders</h2>
      <Card className="mt-4">
        <p
          role="status"
          className={`text-body ${batch === null ? 'text-dim' : batchColour(batch)}`}
        >
          {placing.phase === 'deciding'
            ? 'Recording the approval…'
            : batch === null
              ? 'Reading the batch…'
              : `${batchLabel(batch)} · ${sent} of ${batch.orders.length} ${
                  batch.orders.length === 1 ? 'order' : 'orders'
                } sent`}
        </p>
        <p className="mt-2 text-label text-faint">
          The approval is recorded first, and a worker on the server then submits the orders on its
          own thread. This page is re-reading the batch until it stops rather than sending anything
          itself, so an order that is slow to fill is the broker taking its time rather than a wait
          invented here. Nothing is sent to a broker by this browser.
        </p>
      </Card>
    </section>
  )
}

/** The proposal section, or nothing at all.
 *
 * There is deliberately no placeholder before a proposal exists: an empty frame on a page whose
 * purpose is news would be a section that says "something is missing", and nothing is.
 */
function ProposalPanel({
  state,
  dismissed,
  onDismiss,
  onRetry,
  deciding,
  notice,
  onDecide,
}: {
  state: ProposalState
  dismissed: boolean
  onDismiss: () => void
  onRetry: () => void
  deciding: boolean
  notice: string | null
  onDecide: (decision: 'approve' | 'reject', confirm: boolean) => void
}) {
  if (state.phase === 'restoring' || state.phase === 'idle') return null

  if (state.phase === 'generating') {
    return (
      <section className="mt-6 border-t border-line pt-6">
        <h2 className="text-heading font-semibold text-ink">Rebalance proposal</h2>
        <Card className="mt-4">
          <p role="status" className="text-body text-dim">
            {state.stage === null
              ? 'Starting…'
              : (STAGE_LABELS[state.stage] ?? state.stage)}
          </p>
          <p className="mt-1 text-label text-faint">
            The portfolio is read from the broker first, then the stored news is searched for
            relevant evidence, then the target weights are proposed and the trades calculated.
            Nothing is sent to a broker at any point.
          </p>
        </Card>
      </section>
    )
  }

  if (state.phase === 'error') {
    return (
      <section className="mt-6 border-t border-line pt-6">
        <h2 className="text-heading font-semibold text-ink">Rebalance proposal</h2>
        <div className="mt-4">
          <ErrorNotice
            title="Could not generate a proposal."
            message={state.message}
            onRetry={onRetry}
          />
        </div>
      </section>
    )
  }

  if (dismissed) return null

  const decided = state.proposal.decision
  const askedAgain = notice !== null && /refreshed|moved/i.test(notice)

  return (
    <ProposalSection
      proposal={state.proposal}
      onDismiss={onDismiss}
      deciding={deciding}
      notice={notice}
      // A renewal has already been asked once, so the next press is the confirmation. Any
      // other press is a first decision.
      onApprove={() => onDecide('approve', askedAgain && decided === null)}
      onReject={() => onDecide('reject', false)}
    />
  )
}

/** What an ingestion did, in a sentence. A partial failure is reported as one rather than
 *  as an error: the run completed and most of it worked, and naming the source that did
 *  not is the useful part. */
function describeIngest(result: NewsIngest): string {
  const parts = [
    `Read ${result.sources.length} sources: ${result.stored} stored, ${result.indexed} indexed.`,
  ]
  if (result.failed_sources.length > 0) {
    const named = result.failed_sources.map(labelFor).join(', ')
    parts.push(`Could not read: ${named}. Everything else was stored.`)
  }
  const problems = result.sources.filter((item) => item.error !== null && item.status === 'ok')
  for (const item of problems) {
    parts.push(`${labelFor(item.source)}: ${item.error}`)
  }
  return parts.join(' ')
}

function SearchResults({ search }: { search: SearchState }) {
  if (search.phase === 'searching') {
    return (
      <Card>
        <p role="status" className="text-body text-dim">
          Searching…
        </p>
      </Card>
    )
  }

  if (search.phase === 'error' || search.phase === 'idle') {
    return (
      <Card>
        <p role="alert" className="text-body text-danger">
          {search.phase === 'error' ? search.message : ''}
        </p>
      </Card>
    )
  }

  const { result } = search

  // Every status but `ok` is its own answer, and the two that look alike from a distance
  // -- nothing matched, and nothing is indexed -- call for entirely different actions.
  if (result.status !== 'ok') {
    return (
      <Card>
        <p className="text-body text-ink">{explainSearchStatus(result)}</p>
        {result.reason !== null && <p className="mt-1 text-note text-dim">{result.reason}</p>}
      </Card>
    )
  }

  return (
    <>
      <p className="text-note text-dim">
        {result.returned} passage{result.returned === 1 ? '' : 's'} similar to{' '}
        <span className="text-ink">“{result.query}”</span>
      </p>
      {result.passages.map((passage) => (
        <Card key={`${passage.article.id}-${passage.chunk_index}`}>
          <ArticleHeading article={passage.article} />
          <p className="mt-2 text-body text-dim">{passage.text}</p>
          <p className="mt-2 text-label text-faint">
            Similarity {passage.similarity.toFixed(3)}
          </p>
        </Card>
      ))}
      {result.warnings.map((warning) => (
        <p key={warning} className="text-label text-faint">
          {warning}
        </p>
      ))}
    </>
  )
}

function explainSearchStatus(result: NewsSearch): string {
  switch (result.status) {
    case 'no_matching_results':
      return 'Nothing stored is similar to that.'
    case 'nothing_indexed':
      return 'No news has been indexed yet, so there is nothing to search. Refresh news first.'
    case 'index_unavailable':
      return 'The search index is unavailable. The stored articles are unaffected.'
    case 'model_unavailable':
      return 'The embedding model is unavailable, so the search cannot run.'
    default:
      return 'The search could not be completed.'
  }
}

function LatestArticles({
  state,
  onLoadMore,
  loadingMore,
  filtered,
}: {
  state: ListState
  onLoadMore: () => void
  loadingMore: boolean
  /** Whether a Source or Category filter is narrowing the listing.
   *
   * It changes what an empty page means, and the two are not interchangeable: "nothing has
   * been read from the sources" and "nothing matches what you asked for" call for entirely
   * different next steps, and telling someone the database is empty when they have simply
   * filtered everything out sends them to refresh a feed that is working fine. */
  filtered: boolean
}) {
  if (state.phase === 'loading') {
    return (
      <Card>
        <p role="status" className="text-body text-dim">
          Loading news…
        </p>
      </Card>
    )
  }

  if (state.phase !== 'ready' && state.phase !== 'stale') return null

  const { list } = state

  return (
    <>
      <SourceTimes list={list} />

      {list.articles.length === 0 ? (
        <Card>
          <p className="text-body text-ink">
            {filtered ? 'No articles match your filters.' : 'No news stored yet.'}
          </p>
          <p className="mt-1 text-note text-dim">
            {filtered
              ? 'Articles are stored — none of them match the Source and Category you selected. Show all sources and categories to see them.'
              : 'Nothing has been read from the sources. Choose “Refresh news” to read them.'}
          </p>
        </Card>
      ) : (
        list.articles.map((article) => (
          <Card key={article.id}>
            <ArticleHeading article={article} />
            <p className="mt-2 text-body text-dim">{article.excerpt}</p>
            {article.symbols.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {article.symbols.map((symbol) => (
                  <span
                    key={symbol}
                    className="rounded-full bg-selected px-2 py-0.5 text-label text-accent-ink"
                  >
                    {symbol}
                  </span>
                ))}
              </div>
            )}
          </Card>
        ))
      )}

      {list.articles.length < list.total && (
        <Button
          onClick={() => {
            if (!loadingMore) onLoadMore()
          }}
          aria-disabled={loadingMore}
          className={loadingMore ? 'opacity-50' : ''}
        >
          {loadingMore ? 'Loading…' : `Load more (${list.total - list.articles.length} left)`}
        </Button>
      )}
    </>
  )
}

function ArticleHeading({ article }: { article: NewsArticle }) {
  const published = formatDateTime(article.published_at)
  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-label text-faint">{article.source}</span>
        {article.category === 'macro' && (
          <span className="rounded-full bg-selected px-2 py-0.5 text-label text-accent-ink">
            Economy &amp; policy
          </span>
        )}
        {/* The publisher's own time. Labelled "Published" so it cannot be read as the last
            time this page looked, which is a different fact and shown separately. */}
        {published !== null && (
          <time dateTime={article.published_at} className="text-label text-faint">
            Published {published}
          </time>
        )}
      </div>

      <h2 className="mt-1.5 text-heading font-medium text-ink">
        {article.url ? (
          // External, and marked as such: a reader should know they are leaving, and a
          // news article's text is not something this application vouches for.
          <a
            href={article.url}
            target="_blank"
            rel="noopener noreferrer"
            className="hover:text-accent-ink"
          >
            {article.title}
          </a>
        ) : (
          article.title
        )}
      </h2>
    </div>
  )
}

/** When each source was last read successfully, and when it was last read at all.
 *
 * From the ingestion receipts rather than from the articles, so a source that was read and
 * had nothing new to say still shows a recent time instead of looking broken.
 */
function SourceTimes({ list }: { list: NewsList }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
      <p className="text-label text-faint">Last successful ingestion:</p>
      {list.sources.map((status) => {
        const at = formatSyncTime(status.last_success_at)
        return (
          <p key={status.source} className="text-label text-faint">
            {labelFor(status.source)} {at ?? 'never'}
          </p>
        )
      })}
    </div>
  )
}
