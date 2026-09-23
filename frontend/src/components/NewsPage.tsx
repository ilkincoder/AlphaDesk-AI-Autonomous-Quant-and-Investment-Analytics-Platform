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

import { fetchNews, fetchProposal, ingestNews, searchNews, streamProposal } from '../api'
import type {
  NewsArticle,
  NewsIngest,
  NewsList,
  NewsSearch,
  RebalanceProposal,
} from '../api'
import { formatDateTime, formatSyncTime } from '../format'
import { Button } from './Button'
import { Card } from './Card'
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

export function NewsPage({ active = true }: { active?: boolean }) {
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
  // nothing about it reaches a broker: there is no order to cancel.
  const [dismissed, setDismissed] = useState(false)

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

  /** Ask for a proposal, and say which stage the run is in while it works. */
  const generate = useCallback(async () => {
    if (generating.current) return
    generating.current = true
    setDismissed(false)
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
  const busy = proposal.phase === 'generating'

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
          {/* The page's one primary action, and the only thing here that runs the agent. It
              generates a proposal and stops: approving and executing are not built, so there is
              no second purple button beside it. */}
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
        </div>
      </header>

      {/* A proposal takes a minute and is worth waiting for, so what it is doing is named
          rather than shown as a spinner. `role="status"` because it accompanies the news below
          and should not interrupt. */}
      {busy && (
        <p role="status" className="mt-3 text-note text-dim">
          {proposal.stage === null
            ? 'Starting…'
            : (STAGE_LABELS[proposal.stage] ?? proposal.stage)}
          . The news below stays available while this runs.
        </p>
      )}

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

      {/* Below the news and its Load more, not instead of them: the page keeps doing what it
          did, and the proposal is added under it. */}
      <ProposalPanel
        state={proposal}
        dismissed={dismissed}
        onDismiss={() => setDismissed(true)}
        onRetry={() => void generate()}
      />
    </div>
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
}: {
  state: ProposalState
  dismissed: boolean
  onDismiss: () => void
  onRetry: () => void
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

  return <ProposalSection proposal={state.proposal} onDismiss={onDismiss} />
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
