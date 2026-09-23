/** The News page, driven entirely by stubbed responses.
 *
 * Nothing here touches the API, a provider or the index: every case is a fixture handed to
 * a stubbed `fetch`, routed by URL. That is the only way to reach the empty, partial-failure,
 * stale and index-unavailable states without editing stored data or spending a request.
 *
 * The states carry the weight. "Nothing matched", "nothing is indexed" and "the index is
 * down" are three different things, and a page that showed the same message for all three
 * would be wrong about two of them.
 */

import { StrictMode } from 'react'

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { NewsIngest, NewsList, NewsSearch } from '../api'
import { aProposal, sseResponse } from '../testing'
import { NewsPage } from './NewsPage'

const PUBLISHED = '2026-09-22T14:00:00+00:00'
const INGESTED = '2026-09-22T15:30:00+00:00'

/** The shape `GET /news` returns: one company story and one macro release. */
const LIST: NewsList = {
  total: 2,
  limit: 20,
  offset: 0,
  timeliness:
    'News from Alpaca is provided by Benzinga and is delayed: without real-time entitlement ' +
    'Alpaca documents a window ending fifteen minutes before the request.',
  sources: [
    { source: 'alpaca_news', last_success_at: INGESTED },
    { source: 'official_fed_monetary', last_success_at: INGESTED },
    { source: 'official_bls_cpi', last_success_at: null },
  ],
  articles: [
    {
      id: 1,
      provider: 'alpaca_news',
      source: 'benzinga',
      category: 'company',
      title: 'Analyst Sees More Upside for Microsoft',
      excerpt: 'A body about MSFT and data centres.',
      url: 'https://www.benzinga.com/story/1',
      symbols: ['MSFT'],
      published_at: PUBLISHED,
      provider_updated_at: null,
      ingested_at: INGESTED,
      indexed: true,
    },
    {
      id: 2,
      provider: 'official_fed_monetary',
      source: 'Federal Reserve',
      category: 'macro',
      title: 'Federal Reserve issues FOMC statement',
      excerpt: 'Recent indicators suggest that economic activity has continued to expand.',
      url: 'https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm',
      symbols: [],
      published_at: '2026-09-16T18:00:00+00:00',
      provider_updated_at: null,
      ingested_at: INGESTED,
      indexed: true,
    },
  ],
}

const INGEST: NewsIngest = {
  started_at: INGESTED,
  completed_at: INGESTED,
  symbols: ['AAPL', 'MSFT'],
  stored: 3,
  indexed: 3,
  failed_sources: [],
  sources: [
    {
      source: 'alpaca_news',
      status: 'ok',
      fetched: 3,
      new: 2,
      updated: 0,
      unchanged: 1,
      indexed: 3,
      failed_index: 0,
      error: null,
      last_success_at: INGESTED,
    },
    {
      source: 'official_fed_monetary',
      status: 'ok',
      fetched: 1,
      new: 1,
      updated: 0,
      unchanged: 0,
      indexed: 1,
      failed_index: 0,
      error: null,
      last_success_at: INGESTED,
    },
  ],
}

const SEARCH: NewsSearch = {
  status: 'ok',
  reason: null,
  query: 'data centres',
  returned: 1,
  passages: [
    {
      text: 'Microsoft raised its data centre spending forecast.',
      similarity: 0.71,
      chunk_index: 0,
      article: LIST.articles[0],
    },
  ],
  warnings: ['These are passages retrieved by similarity.'],
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

/** The same listing narrowed to one feed, as `?provider=` returns it. */
const FED_ONLY: NewsList = {
  ...LIST,
  total: 1,
  articles: LIST.articles.filter((article) => article.provider === 'official_fed_monetary'),
}

/** A `fetch` that answers only when the test says so, and that honours the request's abort
 *  signal the way a real one does.
 *
 * `stubApi` resolves whatever happens to the signal, and that is exactly why no test here
 * caught either bug: a page that cancelled its own request still looked like it had loaded,
 * and a request that had been superseded still delivered its answer. `release` is the test
 * letting the answers through, in whatever order it wants them to land.
 */
function controllableApi(respond: (url: string) => unknown) {
  const calls: string[] = []
  const proposal: unknown = { proposal: null }
  const release: Array<() => void> = []
  const mock = vi.fn((url: string, init?: RequestInit) => {
    calls.push(url)
    return new Promise<Response>((resolve, reject) => {
      const signal = init?.signal
      const onAbort = () => reject(new DOMException('aborted', 'AbortError'))
      if (signal?.aborted) {
        onAbort()
        return
      }
      signal?.addEventListener('abort', onAbort, { once: true })
      const body = url.startsWith('/api/rebalance/proposal') ? proposal : respond(url)
      release.push(() => resolve(jsonResponse(body)))
    })
  })
  vi.stubGlobal('fetch', mock)
  return { mock, calls, release }
}

/** Let every answer through, including the ones that are only asked for once an earlier
 *  answer lands -- a portfolio load is a sync and then a read, and a refresh is an
 *  ingestion and then a listing. `release` grows as it is drained, so the loop re-reads it.
 */
async function releaseAll(release: Array<() => void>) {
  await act(async () => {
    for (let index = 0; index < release.length; index++) {
      release[index]()
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })
}

const listingCalls = (calls: string[]) => calls.filter((url) => url.startsWith('/api/news?'))
const proposalReads = (urls: string[]) =>
  urls.filter((url) => url === '/api/rebalance/proposal')
const proposalRuns = (urls: string[]) =>
  urls.filter((url) => url.startsWith('/api/rebalance/proposal/stream'))


type Route = () => Response | Promise<Response>

/** Route by URL, because this page calls four endpoints and the order they are reached
 *  depends on what the reader does.
 *
 * The proposal route answers "there is none" by default, which is what a fresh install returns
 * and what most of these tests are about: the page reads it on mount, and a test that forgot to
 * route it would be testing a page that crashed on load rather than the thing it meant to.
 */
function stubApi(
  routes: { list?: Route; search?: Route; ingest?: Route; proposal?: Route; generate?: Route } = {},
) {
  const calls: string[] = []
  // The same calls with their request bodies, for the few assertions that are about what was
  // *sent* rather than where. `calls` stays a list of URLs because most of this file filters it.
  const requests: Array<{ url: string; init?: RequestInit }> = []
  const mock = vi.fn((url: string, init?: RequestInit) => {
    calls.push(url)
    requests.push({ url, init })
    if (url.startsWith('/api/news/ingest')) {
      return Promise.resolve((routes.ingest ?? (() => jsonResponse(INGEST)))())
    }
    if (url.startsWith('/api/news/search')) {
      return Promise.resolve((routes.search ?? (() => jsonResponse(SEARCH)))())
    }
    if (url.startsWith('/api/rebalance/proposal/stream')) {
      return Promise.resolve(
        (routes.generate ?? (() => sseResponse([{ event: 'done', proposal: aProposal() }])))()
      )
    }
    if (url.startsWith('/api/rebalance/proposal')) {
      return Promise.resolve((routes.proposal ?? (() => jsonResponse({ proposal: null })))())
    }
    return Promise.resolve((routes.list ?? (() => jsonResponse(LIST)))())
  })
  vi.stubGlobal('fetch', mock)
  return { mock, calls, requests }
}

async function renderLoaded(routes = {}) {
  const api = stubApi(routes)
  render(<NewsPage />)
  await screen.findByText('Analyst Sees More Upside for Microsoft')
  return api
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('NewsPage', () => {
  it('says it is loading before anything arrives, and reads nothing on its own', () => {
    stubApi({ list: () => new Promise(() => {}) as unknown as Response })

    render(<NewsPage />)

    expect(screen.getByText('Loading news…')).toBeTruthy()
  })

  it('loads on the first visit, with StrictMode running the effect twice', async () => {
    // The bug this pins: the effect aborted its own request on StrictMode's second run and
    // the in-flight guard refused to start another, so the page waited for ever. No test
    // here rendered under StrictMode, and the other doubles ignore the abort signal.
    const api = controllableApi(() => LIST)

    render(
      <StrictMode>
        <NewsPage />
      </StrictMode>,
    )
    await releaseAll(api.release)

    expect(await screen.findByText('Analyst Sees More Upside for Microsoft')).toBeTruthy()
  })

  it('asks again when a filter changes while a request is still in flight', async () => {
    // The bug this pins: the second change was dropped because the first request had not
    // finished, so the listing showed the previous selection's articles underneath the new
    // selection's controls -- and nothing on screen said so.
    const api = controllableApi((url) =>
      url.includes('provider=official_fed_monetary') ? FED_ONLY : LIST,
    )

    render(<NewsPage />)
    fireEvent.change(screen.getByLabelText('Source'), {
      target: { value: 'official_fed_monetary' },
    })

    await waitFor(() => expect(listingCalls(api.calls)).toHaveLength(2))
    expect(listingCalls(api.calls)[1]).toContain('provider=official_fed_monetary')

    // The first request is answered late, after the selection moved on. It was asked for
    // with the previous selection, so its answer must not land on top of the newer one.
    await releaseAll(api.release)

    expect(
      await screen.findByText('Federal Reserve issues FOMC statement'),
    ).toBeTruthy()
    expect(screen.queryByText('Analyst Sees More Upside for Microsoft')).toBeNull()
  })

  it('reads the listing back with the filter as it is when a refresh finishes', async () => {
    // A refresh is started by the render that was on screen when the button was pressed, so
    // the selection can move while it runs. Reading the listing back with the selection it
    // started with is how the page ends up disagreeing with its own controls.
    const api = controllableApi((url) => {
      if (url.includes('/news/ingest')) return INGEST
      return url.includes('provider=official_fed_monetary') ? FED_ONLY : LIST
    })

    render(<NewsPage />)
    await releaseAll(api.release)
    await screen.findByText('Analyst Sees More Upside for Microsoft')

    fireEvent.click(screen.getByRole('button', { name: 'Refresh news' }))
    fireEvent.change(screen.getByLabelText('Source'), {
      target: { value: 'official_fed_monetary' },
    })

    await waitFor(() => expect(api.calls.some((url) => url.includes('/news/ingest'))).toBe(true))
    await releaseAll(api.release)

    await waitFor(() => {
      const listing = listingCalls(api.calls)
      expect(listing[listing.length - 1]).toContain('provider=official_fed_monetary')
    })
    expect(await screen.findByText('Federal Reserve issues FOMC statement')).toBeTruthy()
    expect(screen.queryByText('Analyst Sees More Upside for Microsoft')).toBeNull()
    // And the run still reports what it did -- read back with the selection as it is now,
    // the notice is a fact about the run and outlives the listing underneath it.
    expect(screen.getByText(/Read 2 sources: 3 stored, 3 indexed/)).toBeTruthy()
  })

  it('renders every article with its source, headline, excerpt and link', async () => {
    await renderLoaded()

    const apple = screen.getByText('Analyst Sees More Upside for Microsoft')
    expect(apple.closest('a')?.getAttribute('href')).toBe('https://www.benzinga.com/story/1')
    expect(apple.closest('a')?.getAttribute('rel')).toContain('noopener')
    expect(screen.getByText('A body about MSFT and data centres.')).toBeTruthy()
    expect(screen.getByText('benzinga')).toBeTruthy()
    // `getAllBy`: a source name appears both on its article and in the last-ingestion
    // line, and "found two" is not a failure here.
    expect(screen.getAllByText('Federal Reserve').length).toBeGreaterThan(0)
    expect(screen.getByText('MSFT')).toBeTruthy()
  })

  it('distinguishes the publication time from the last ingestion time', async () => {
    await renderLoaded()

    // The publisher's own instant, carried in the markup rather than the moment we stored
    // it -- and the two are different values in this fixture.
    const times = Array.from(document.querySelectorAll('time'))
    expect(times.map((time) => time.getAttribute('dateTime'))).toContain(PUBLISHED)
    expect(times.map((time) => time.getAttribute('dateTime'))).not.toContain(INGESTED)

    // And ours is labelled separately, so neither can be read as the other.
    expect(screen.getByText('Last successful ingestion:')).toBeTruthy()
    // Also in the source filter's options, hence `getAllBy`.
    expect(screen.getAllByText(/Alpaca \(Benzinga\)/).length).toBeGreaterThan(0)
    // A source that has never been read says so rather than showing a time.
    expect(screen.getByText(/BLS — CPI never/)).toBeTruthy()
  })

  it('does not claim the feed is live', async () => {
    await renderLoaded()

    expect(screen.getByText('Delayed')).toBeTruthy()
    expect(screen.queryByText(/real-?time/i)).toBeNull()
    expect(screen.queryByText('Live')).toBeNull()
  })

  it('says nothing is stored yet, and points at Refresh, rather than looking empty', async () => {
    stubApi({ list: () => jsonResponse({ ...LIST, total: 0, articles: [] }) })

    render(<NewsPage />)

    expect(await screen.findByText('No news stored yet.')).toBeTruthy()
    expect(screen.getByText(/Choose “Refresh news”/)).toBeTruthy()
  })

  it('reads the sources on Refresh and reports what each one did', async () => {
    const { calls } = await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh news' }))

    expect(await screen.findByText(/3 stored, 3 indexed/)).toBeTruthy()
    expect(calls.filter((url) => url.startsWith('/api/news/ingest'))).toHaveLength(1)
    // And the listing is reloaded afterwards, so the page shows what was just stored.
    expect(calls.filter((url) => url.startsWith('/api/news?')).length).toBeGreaterThan(1)
  })

  it('reports a partial failure without discarding what did load', async () => {
    const partial: NewsIngest = {
      ...INGEST,
      stored: 1,
      failed_sources: ['official_bls_cpi'],
      sources: [
        INGEST.sources[0],
        {
          source: 'official_bls_cpi',
          status: 'failed',
          fetched: 0,
          new: 0,
          updated: 0,
          unchanged: 0,
          indexed: 0,
          failed_index: 0,
          error: 'Could not reach bls.gov.',
          last_success_at: null,
        },
      ],
    }
    await renderLoaded({ ingest: () => jsonResponse(partial) })

    fireEvent.click(screen.getByRole('button', { name: 'Refresh news' }))

    expect(await screen.findByText(/Could not read: BLS — CPI/)).toBeTruthy()
    expect(screen.getByText(/Everything else was stored/)).toBeTruthy()
    // The articles are still on screen: a partial failure is not an empty page.
    expect(screen.getByText('Analyst Sees More Upside for Microsoft')).toBeTruthy()
  })

  it('keeps the articles and says so when a refresh fails outright', async () => {
    await renderLoaded({
      ingest: () => jsonResponse({ detail: 'No news source could be read.' }, 503),
    })

    fireEvent.click(screen.getByRole('button', { name: 'Refresh news' }))

    expect(await screen.findByText(/Showing previously loaded articles/)).toBeTruthy()
    expect(screen.getByText(/No news source could be read/)).toBeTruthy()
    expect(screen.getByText('Analyst Sees More Upside for Microsoft')).toBeTruthy()
  })

  it('starts one refresh at a time however many times it is clicked', async () => {
    // An ingestion that never settles: the page is mid-refresh for the whole test.
    const { calls } = await renderLoaded({
      ingest: () => new Promise(() => {}) as unknown as Response,
    })

    const button = screen.getByRole('button', { name: /Refresh news|Refreshing/ })
    fireEvent.click(button)
    fireEvent.click(button)
    fireEvent.click(button)

    expect(calls.filter((url) => url.startsWith('/api/news/ingest'))).toHaveLength(1)
  })

  it('sends the query and shows the passages it came back with', async () => {
    const { calls } = await renderLoaded()

    fireEvent.change(screen.getByLabelText('Search the stored news'), {
      target: { value: 'data centres' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText(/Microsoft raised its data centre spending/)).toBeTruthy()
    expect(screen.getByText(/Similarity 0.710/)).toBeTruthy()
    expect(calls.some((url) => url.startsWith('/api/news/search?q=data+centres'))).toBe(true)
  })

  it('reports nothing matching as its own answer, not as an empty page', async () => {
    await renderLoaded({
      search: () => jsonResponse({ ...SEARCH, status: 'no_matching_results', returned: 0, passages: [] }),
    })

    fireEvent.change(screen.getByLabelText('Search the stored news'), {
      target: { value: 'quantum chromodynamics' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText('Nothing stored is similar to that.')).toBeTruthy()
  })

  it('reports an unavailable index differently from no results', async () => {
    await renderLoaded({
      search: () =>
        jsonResponse({ ...SEARCH, status: 'index_unavailable', reason: 'Qdrant is unreachable', returned: 0, passages: [] }),
    })

    fireEvent.change(screen.getByLabelText('Search the stored news'), {
      target: { value: 'anything' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText(/search index is unavailable/)).toBeTruthy()
    expect(screen.queryByText('Nothing stored is similar to that.')).toBeNull()
  })

  it('reports nothing indexed as its own answer too', async () => {
    await renderLoaded({
      search: () => jsonResponse({ ...SEARCH, status: 'nothing_indexed', returned: 0, passages: [] }),
    })

    fireEvent.change(screen.getByLabelText('Search the stored news'), {
      target: { value: 'anything' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText(/No news has been indexed yet/)).toBeTruthy()
  })

  it('sends the provider and category filters with the listing', async () => {
    const { calls } = await renderLoaded()

    fireEvent.change(screen.getByLabelText('Source'), {
      target: { value: 'official_fed_monetary' },
    })
    await waitFor(() =>
      expect(calls.some((url) => url.includes('provider=official_fed_monetary'))).toBe(true),
    )

    fireEvent.change(screen.getByLabelText('Category'), { target: { value: 'macro' } })
    await waitFor(() =>
      expect(
        calls.some(
          (url) => url.includes('category=macro') && url.includes('provider=official_fed_monetary'),
        ),
      ).toBe(true),
    )
  })

  it('omits a filter that is not set rather than sending it blank', async () => {
    const { calls } = await renderLoaded()

    const listing = calls.find((url) => url.startsWith('/api/news?'))!
    expect(listing).not.toContain('provider=')
    expect(listing).not.toContain('category=')
    expect(listing).toContain('limit=20')
  })

  it('appends the next page rather than replacing what is on screen', async () => {
    const second = {
      ...LIST,
      offset: 2,
      articles: [{ ...LIST.articles[0], id: 3, title: 'A third story' }],
    }
    let listingCall = 0
    const { calls } = await renderLoaded({
      list: () => {
        listingCall += 1
        return jsonResponse(listingCall === 1 ? { ...LIST, total: 3 } : second)
      },
    })

    fireEvent.click(screen.getByRole('button', { name: /Load more/ }))

    expect(await screen.findByText('A third story')).toBeTruthy()
    expect(screen.getByText('Analyst Sees More Upside for Microsoft')).toBeTruthy()
    expect(calls.some((url) => url.includes('offset=2'))).toBe(true)
    // And it is gone once there is nothing left to fetch.
    expect(screen.queryByRole('button', { name: /Load more/ })).toBeNull()
  })

  it('names the page News & Trading and keeps the news controls', async () => {
    await renderLoaded()

    expect(screen.getByRole('heading', { level: 1, name: 'News & Trading' })).toBeTruthy()
    expect(screen.getByLabelText('Search the stored news')).toBeTruthy()
    expect(screen.getByLabelText('Source')).toBeTruthy()
    expect(screen.getByLabelText('Category')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Refresh news' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Rebalance proposal' })).toBeTruthy()
  })

  it('says nothing about a proposal until there is one', async () => {
    await renderLoaded()

    // An empty frame on a page whose purpose is news would be a section that says something is
    // missing, and on a fresh install nothing is.
    expect(screen.queryByRole('heading', { name: 'Rebalance proposal' })).toBeNull()
    expect(screen.queryByText(/No orders have been sent/)).toBeNull()
  })

  it('restores the latest proposal on load, without generating anything', async () => {
    const { calls } = stubApi({ proposal: () => jsonResponse({ proposal: aProposal() }) })

    render(<NewsPage />)

    expect(await screen.findByRole('heading', { name: 'Rebalance proposal' })).toBeTruthy()
    // Twice: the panel's status line and the summary card's recorded status.
    expect(screen.getAllByText('Proposed · Estimates only')).toHaveLength(2)
    // Restoring is a read. Nothing was generated and nothing was spent.
    expect(proposalRuns(calls)).toHaveLength(0)
  })

  it('restores the proposal once under StrictMode, and does not generate one', async () => {
    const { calls } = stubApi({ proposal: () => jsonResponse({ proposal: aProposal() }) })

    render(
      <StrictMode>
        <NewsPage />
      </StrictMode>,
    )

    expect(await screen.findByRole('heading', { name: 'Rebalance proposal' })).toBeTruthy()
    expect(proposalRuns(calls)).toHaveLength(0)
  })

  it('disables the button and names the stage while a proposal is being generated', async () => {
    let release: (() => void) | null = null
    const { calls } = await renderLoaded({
      generate: () =>
        new Promise((resolve) => {
          release = () =>
            resolve(
              sseResponse([
                { event: 'stage', stage: 'retrieving_news' },
                { event: 'done', proposal: aProposal() },
              ]),
            )
        }) as unknown as Response,
    })

    fireEvent.click(screen.getByRole('button', { name: 'Rebalance proposal' }))

    const button = await screen.findByRole('button', { name: 'Generating…' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    // The news stays available: the run is not the page.
    expect(screen.getByText('Analyst Sees More Upside for Microsoft')).toBeTruthy()

    await act(async () => {
      release?.()
      await new Promise((resolve) => setTimeout(resolve, 0))
    })

    expect(proposalRuns(calls)).toHaveLength(1)
  })

  it('never starts a second run for a second click', async () => {
    const { calls } = await renderLoaded({
      generate: () =>
        new Promise(() => {
          // Never settles: the run is in flight for the whole test.
        }) as unknown as Response,
    })

    const button = screen.getByRole('button', { name: 'Rebalance proposal' })
    fireEvent.click(button)
    await screen.findByRole('button', { name: 'Generating…' })
    fireEvent.click(screen.getByRole('button', { name: 'Generating…' }))

    expect(proposalRuns(calls)).toHaveLength(1)
  })

  it('does not let a news filter narrow the proposal request', async () => {
    const { calls, requests } = await renderLoaded()

    // Narrow the listing to one feed and one category first.
    fireEvent.change(screen.getByLabelText('Source'), {
      target: { value: 'official_fed_monetary' },
    })
    fireEvent.change(screen.getByLabelText('Category'), { target: { value: 'macro' } })
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })

    fireEvent.click(screen.getByRole('button', { name: 'Rebalance proposal' }))
    await screen.findByRole('heading', { name: 'Rebalance proposal' })

    // The proposal is portfolio-wide. A filter that restricted it would produce a proposal
    // about a different portfolio than the one the page is showing.
    const sent = requests.find((call) =>
      call.url.startsWith('/api/rebalance/proposal/stream'),
    )
    expect(sent).toBeTruthy()
    expect(Object.keys(JSON.parse(String(sent?.init?.body)))).toEqual(['request_id'])
    // One run, and the one read that restored the page's state on mount.
    expect(proposalRuns(calls)).toHaveLength(1)
    expect(proposalReads(calls)).toHaveLength(1)
  })

  it('shows the proposal once the run finishes', async () => {
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Rebalance proposal' }))

    expect((await screen.findAllByText('Proposed · Estimates only')).length).toBe(2)
    // The trades and the target schedule, both rendered from the calculation.
    expect(screen.getByRole('table', { name: /Proposed whole-share orders/ })).toBeTruthy()
    expect(screen.getByRole('table', { name: /Target weights per holding/ })).toBeTruthy()
  })

  it('reports a failed run without losing the news', async () => {
    const { calls } = await renderLoaded({
      generate: () => jsonResponse({ detail: 'The proposal run failed.' }, 503),
    })

    fireEvent.click(screen.getByRole('button', { name: 'Rebalance proposal' }))

    expect(await screen.findByText('Could not generate a proposal.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Rebalance proposal' })).toBeTruthy()
    expect(screen.getByText('Analyst Sees More Upside for Microsoft')).toBeTruthy()
    // A failure is a recorded outcome, not a retry: the page does not ask again on its own.
    expect(proposalRuns(calls)).toHaveLength(1)
  })

  it('hides the panel on Dismiss and brings it back on the next run', async () => {
    await renderLoaded()

    fireEvent.click(screen.getByRole('button', { name: 'Rebalance proposal' }))
    await screen.findByRole('heading', { name: 'Rebalance proposal' })

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('heading', { name: 'Rebalance proposal' })).toBeNull()

    // Dismissing hides the panel. It does not delete the stored proposal, and there is no
    // order behind it to cancel.
    fireEvent.click(screen.getByRole('button', { name: 'Rebalance proposal' }))
    expect(await screen.findByRole('heading', { name: 'Rebalance proposal' })).toBeTruthy()
  })

  it('says a filtered listing matched nothing rather than that the store is empty', async () => {
    const empty: NewsList = { ...LIST, total: 0, articles: [] }
    // The listing goes empty once a filter is applied, which is what a filtered query that
    // matched nothing really returns.
    let filtered = false
    stubApi({ list: () => jsonResponse(filtered ? empty : LIST) })
    render(<NewsPage />)
    await screen.findByText('Analyst Sees More Upside for Microsoft')

    filtered = true
    fireEvent.change(screen.getByLabelText('Source'), {
      target: { value: 'official_fed_monetary' },
    })

    expect(await screen.findByText('No articles match your filters.')).toBeTruthy()
  })

  it('says nothing has been stored when there is no filter to blame', async () => {
    stubApi({ list: () => jsonResponse({ ...LIST, total: 0, articles: [] }) })

    render(<NewsPage />)

    expect(await screen.findByText('No news stored yet.')).toBeTruthy()
  })

  it('reads nothing at all while the page is not the one being shown', () => {
    const { mock } = stubApi()

    render(<NewsPage active={false} />)

    // No request, which is the point: reading a provider is not something a hidden page
    // does. What it renders is the shell's business -- `App` keeps the panel in the DOM
    // with `display: none`, so the placeholder is never on screen.
    expect(mock).not.toHaveBeenCalled()
  })
})
