/** The News page: what has been published about the holdings, and about the economy.
 *
 * Everything here is *read* except one button. "Refresh news" is the only thing that
 * reaches a provider, and it is bounded on the server — an article count, entries per
 * feed, release pages and a window — so pressing it costs a predictable amount. Nothing
 * runs when the page opens: a page that ingested on load would spend a provider's requests
 * on whoever happened to click the sidebar.
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

import { fetchNews, ingestNews, searchNews } from '../api'
import type { NewsArticle, NewsIngest, NewsList, NewsSearch } from '../api'
import { formatDateTime, formatSyncTime } from '../format'
import { Button } from './Button'
import { Card } from './Card'
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
  | { phase: 'ready'; list: NewsList; notice: string | null }
  | { phase: 'stale'; list: NewsList; message: string }
  | { phase: 'error'; message: string }

type SearchState =
  | { phase: 'idle' }
  | { phase: 'searching' }
  | { phase: 'ready'; result: NewsSearch }
  | { phase: 'error'; message: string }

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}

export function NewsPage({ active = true }: { active?: boolean }) {
  const [state, setState] = useState<ListState>({ phase: 'loading' })
  const [search, setSearch] = useState<SearchState>({ phase: 'idle' })
  const [query, setQuery] = useState('')
  const [source, setSource] = useState('')
  const [category, setCategory] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const inFlight = useRef(false)

  const load = useCallback(
    async (signal?: AbortSignal, filters = { source, category }) => {
      if (inFlight.current) return
      inFlight.current = true
      setRefreshing(true)
      try {
        const list = await fetchNews(filters, { limit: PAGE_SIZE }, signal)
        setState((current) => ({
          phase: 'ready',
          list,
          // A refresh that succeeded says so; a plain load has nothing to announce, and
          // an earlier notice is not carried forward as though it had just happened.
          notice: current.phase === 'ready' ? current.notice : null,
        }))
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        const message = messageOf(error)
        setState((current) =>
          current.phase === 'ready' || current.phase === 'stale'
            ? { phase: 'stale', list: current.list, message }
            : { phase: 'error', message },
        )
      } finally {
        inFlight.current = false
        setRefreshing(false)
      }
    },
    [source, category],
  )

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [active, load])

  /** Read every source once, then reload. The only call that reaches a provider. */
  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    setRefreshing(true)
    try {
      const result = await ingestNews()
      const list = await fetchNews({ source, category }, { limit: PAGE_SIZE })
      setState({ phase: 'ready', list, notice: describeIngest(result) })
      setSearch({ phase: 'idle' })
    } catch (error) {
      const message = messageOf(error)
      setState((current) =>
        current.phase === 'ready' || current.phase === 'stale'
          ? { phase: 'stale', list: current.list, message }
          : { phase: 'error', message },
      )
    } finally {
      inFlight.current = false
      setRefreshing(false)
    }
  }, [source, category])

  const runSearch = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const trimmed = query.trim()
      if (!trimmed) return
      setSearch({ phase: 'searching' })
      try {
        const result = await searchNews(trimmed, { source, category })
        setSearch({ phase: 'ready', result })
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setSearch({ phase: 'error', message: messageOf(error) })
      }
    },
    [query, source, category],
  )

  const loadMore = useCallback(async () => {
    if (state.phase !== 'ready' || inFlight.current) return
    inFlight.current = true
    setLoadingMore(true)
    try {
      const next = await fetchNews(
        { source, category },
        { limit: PAGE_SIZE, offset: state.list.articles.length },
      )
      setState({
        phase: 'ready',
        list: { ...next, articles: [...state.list.articles, ...next.articles] },
        notice: state.notice,
      })
    } catch (error) {
      setState({ phase: 'stale', list: state.list, message: messageOf(error) })
    } finally {
      inFlight.current = false
      setLoadingMore(false)
    }
  }, [state, source, category])

  const searching = search.phase !== 'idle'

  return (
    <div>
      <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div>
          <h1 className="text-page font-bold text-ink">News</h1>
          <p className="mt-1 text-note text-dim">
            Coverage of your holdings, and the economic releases that move them.
          </p>
        </div>
        <div className="flex items-center gap-3">
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
        </div>
      </header>

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
          <label htmlFor="news-source" className="text-label text-faint">
            Source
          </label>
          <select
            id="news-source"
            value={source}
            onChange={(event) => setSource(event.target.value)}
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

        {state.phase === 'ready' && state.notice !== null && (
          <Banner>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <span>{state.notice}</span>
              {/* Through the updater rather than a spread of `state`: the narrowed value
                  is a `ready` state here, and the updater keeps a stale render from
                  writing it back over whatever the page has moved on to. */}
              <Button
                className="bg-transparent"
                onClick={() =>
                  setState((current) =>
                    current.phase === 'ready' ? { ...current, notice: null } : current,
                  )
                }
              >
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
          />
        )}
      </div>
    </div>
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
}: {
  state: ListState
  onLoadMore: () => void
  loadingMore: boolean
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
          <p className="text-body text-ink">No news stored yet.</p>
          <p className="mt-1 text-note text-dim">
            Nothing has been read from the sources. Choose “Refresh news” to read them.
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
