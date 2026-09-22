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

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { NewsIngest, NewsList, NewsSearch } from '../api'
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

type Route = () => Response | Promise<Response>

/** Route by URL, because this page calls three endpoints and the order they are reached
 *  depends on what the reader does. */
function stubApi(routes: { list?: Route; search?: Route; ingest?: Route } = {}) {
  const calls: string[] = []
  const mock = vi.fn((url: string) => {
    calls.push(url)
    if (url.startsWith('/api/news/ingest')) {
      return Promise.resolve((routes.ingest ?? (() => jsonResponse(INGEST)))())
    }
    if (url.startsWith('/api/news/search')) {
      return Promise.resolve((routes.search ?? (() => jsonResponse(SEARCH)))())
    }
    return Promise.resolve((routes.list ?? (() => jsonResponse(LIST)))())
  })
  vi.stubGlobal('fetch', mock)
  return { mock, calls }
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

  it('sends the source and category filters with the listing', async () => {
    const { calls } = await renderLoaded()

    fireEvent.change(screen.getByLabelText('Source'), {
      target: { value: 'official_fed_monetary' },
    })
    await waitFor(() =>
      expect(calls.some((url) => url.includes('source=official_fed_monetary'))).toBe(true),
    )

    fireEvent.change(screen.getByLabelText('Category'), { target: { value: 'macro' } })
    await waitFor(() =>
      expect(
        calls.some(
          (url) => url.includes('category=macro') && url.includes('source=official_fed_monetary'),
        ),
      ).toBe(true),
    )
  })

  it('omits a filter that is not set rather than sending it blank', async () => {
    const { calls } = await renderLoaded()

    const listing = calls.find((url) => url.startsWith('/api/news?'))!
    expect(listing).not.toContain('source=')
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

  it('reads nothing at all while the page is not the one being shown', () => {
    const { mock } = stubApi()

    render(<NewsPage active={false} />)

    // No request, which is the point: reading a provider is not something a hidden page
    // does. What it renders is the shell's business -- `App` keeps the panel in the DOM
    // with `display: none`, so the placeholder is never on screen.
    expect(mock).not.toHaveBeenCalled()
  })
})
