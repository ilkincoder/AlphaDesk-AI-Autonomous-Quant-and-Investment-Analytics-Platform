/** Portfolio page behaviour, driven entirely by stubbed responses.
 *
 * Nothing here touches the real API, the seeded portfolio or Alpaca: every case is a
 * fixture handed to a stubbed `fetch`, which is the only way to reach the failure, empty,
 * and zero-value states without editing persistent data or spending a broker request.
 *
 * Every load is **two calls** now — a sync, then a read — so `stubLoads` pairs them. A
 * test that hands out a single response is testing a page that no longer exists.
 */

import { StrictMode } from 'react'

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SyncResult, Valuation } from '../api'
import { PortfolioPage } from './PortfolioPage'

/** Byte-for-byte the shape `GET /portfolio/valuation` returns before the first sync. */
const DEMO: Valuation = {
  portfolio_id: 1,
  currency: 'USD',
  price_source: 'demo',
  last_synced_at: null,
  cash_balance: '10000.00',
  holdings_value: '6100.00',
  total_value: '16100.00',
  cash_allocation_percent: '62.11',
  holdings: [
    {
      symbol: 'AAPL',
      quantity: '5.000000',
      price: '200.00',
      holding_value: '1000.00',
      allocation_percent: '6.21',
    },
    {
      symbol: 'MSFT',
      quantity: '8.000000',
      price: '450.00',
      holding_value: '3600.00',
      allocation_percent: '22.36',
    },
    {
      symbol: 'NVDA',
      quantity: '10.000000',
      price: '150.00',
      holding_value: '1500.00',
      allocation_percent: '9.32',
    },
  ],
}

/** The same portfolio after a successful sync: the broker's prices, and the broker's own
 *  equity as the total rather than the sum of the parts. */
const SYNCED: Valuation = {
  ...DEMO,
  price_source: 'alpaca_paper',
  last_synced_at: '2026-09-22T14:30:00+00:00',
  cash_balance: '2500.25',
  holdings_value: '6874.25',
  total_value: '9374.50',
  cash_allocation_percent: '26.67',
  holdings: [
    {
      symbol: 'AAPL',
      quantity: '5.000000',
      price: '201.25',
      holding_value: '1006.25',
      allocation_percent: '10.73',
    },
    {
      symbol: 'MSFT',
      quantity: '8.000000',
      price: '510.50',
      holding_value: '4084.00',
      allocation_percent: '43.57',
    },
    {
      symbol: 'NVDA',
      quantity: '10.000000',
      price: '178.40',
      holding_value: '1784.00',
      allocation_percent: '19.03',
    },
  ],
}

const SYNC_OK: SyncResult = {
  applied: true,
  portfolio_id: 1,
  broker: 'alpaca_paper',
  broker_account_id: '8f3a2b10-4c5d-4e6f-8a9b-0c1d2e3f4a5b',
  last_synced_at: '2026-09-22T14:30:00+00:00',
  currency: 'USD',
  cash_balance: '2500.25',
  equity: '9374.50',
  position_count: 3,
}

/** Only the parts of Response the api module reads. Cheaper and more obvious than
 *  building a real Response, which jsdom does not provide. */
function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

/** One load: a successful sync followed by the valuation it produced. */
function stubLoads(...valuations: Response[]) {
  const mock = vi.fn()
  for (const valuation of valuations) {
    mock.mockResolvedValueOnce(jsonResponse(SYNC_OK))
    mock.mockResolvedValueOnce(valuation)
  }
  vi.stubGlobal('fetch', mock)
  return mock
}

/** A `fetch` that answers only when the test says so, and that honours the request's abort
 *  signal the way a real one does.
 *
 * `stubLoads` resolves whatever happens to the signal, and that is exactly why no test here
 * caught the page cancelling its own request: a cancelled fetch that reports success anyway
 * is indistinguishable from a working page. `release` is the test letting the answers
 * through, in whatever order it wants them to land.
 */
function controllableFetch(respond: (url: string) => unknown) {
  const calls: string[] = []
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
      release.push(() => resolve(jsonResponse(respond(url))))
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

/** Wait until `text` is on screen.
 *
 * `findAllBy` rather than `findBy`, because several figures legitimately appear more
 * than once — the total is both the headline value and the Total Value summary, and
 * cash shows in the CASH row, the left card, and the summary.
 */
async function expectVisible(text: string) {
  expect((await screen.findAllByText(text)).length).toBeGreaterThan(0)
}

function expectStillVisible(text: string) {
  expect(screen.getAllByText(text).length).toBeGreaterThan(0)
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('PortfolioPage', () => {
  it('shows card-shaped placeholders before any data arrives', () => {
    // A request that never settles: the page stays in its loading phase.
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))

    render(<PortfolioPage />)

    expect(screen.getByText('Loading portfolio data')).toBeTruthy()
    expect(screen.queryByText('$16,100.00')).toBeNull()
  })

  it('loads on the first render, with StrictMode running the effect twice', async () => {
    // The bug this pins: the effect aborted its own sync when StrictMode ran it a second
    // time, and the in-flight guard then refused to start another -- so opening the app
    // left the page on "Loading portfolio data" until the thirty-second tick happened to
    // rescue it. Nothing here rendered under StrictMode before, and the other doubles
    // ignore the abort signal, so a cancelled request looked like a successful one.
    const api = controllableFetch((url) => (url.includes('sync') ? SYNC_OK : DEMO))

    render(
      <StrictMode>
        <PortfolioPage />
      </StrictMode>,
    )
    await releaseAll(api.release)

    await expectVisible('$16,100.00')
  })

  it('renders every figure from the API response', async () => {
    stubLoads(jsonResponse(DEMO))

    render(<PortfolioPage />)

    // Headline and totals.
    await expectVisible('$16,100.00')
    for (const amount of ['$6,100.00', '$10,000.00']) {
      expect(screen.getAllByText(amount).length).toBeGreaterThan(0)
    }

    // One row per holding, with shares, price, value, and allocation.
    const apple = screen.getByRole('row', { name: /AAPL/ })
    expect(within(apple).getByText('5')).toBeTruthy()
    expect(within(apple).getByText('$200.00')).toBeTruthy()
    expect(within(apple).getByText('$1,000.00')).toBeTruthy()
    expect(within(apple).getByText('6.21%')).toBeTruthy()

    const microsoft = screen.getByRole('row', { name: /MSFT/ })
    expect(within(microsoft).getByText('8')).toBeTruthy()
    expect(within(microsoft).getByText('$3,600.00')).toBeTruthy()
    expect(within(microsoft).getByText('22.36%')).toBeTruthy()

    const nvidia = screen.getByRole('row', { name: /NVDA/ })
    expect(within(nvidia).getByText('10')).toBeTruthy()
    expect(within(nvidia).getByText('$1,500.00')).toBeTruthy()
    expect(within(nvidia).getByText('9.32%')).toBeTruthy()
  })

  it('shows cash as a row after the holdings, with no shares or price', async () => {
    stubLoads(jsonResponse(DEMO))

    render(<PortfolioPage />)
    await expectVisible('$16,100.00')

    const cash = screen.getByRole('row', { name: /CASH/ })
    expect(within(cash).getByText('$10,000.00')).toBeTruthy()
    expect(within(cash).getByText('62.11%')).toBeTruthy()
    // Shares and Price are absent for cash, not zero.
    expect(within(cash).getAllByText('—').length).toBe(2)
  })

  it('reports a missing portfolio as "No portfolio available."', async () => {
    stubLoads(
      jsonResponse({ detail: "Portfolio 'Alpaca Paper' has not been seeded." }, 404),
    )

    render(<PortfolioPage />)

    expect(await screen.findByText('No portfolio available.')).toBeTruthy()
    // The backend's own explanation is shown rather than replaced.
    expect(screen.getByText(/has not been seeded/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })

  it('surfaces a server error message and recovers when Retry succeeds', async () => {
    stubLoads(
      jsonResponse({ detail: 'No demo price available for held symbol(s): XYZ.' }, 503),
      jsonResponse(DEMO),
    )

    render(<PortfolioPage />)

    expect(await screen.findByText(/No demo price available/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await expectVisible('$16,100.00')
  })

  it('keeps previous values on screen and labels them when a refresh fails', async () => {
    stubLoads(jsonResponse(DEMO), jsonResponse({ detail: 'backend unavailable' }, 503))

    render(<PortfolioPage />)
    await expectVisible('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    expect(await screen.findByText(/Showing previously loaded values/)).toBeTruthy()
    expect(screen.getByText(/backend unavailable/)).toBeTruthy()
    // The numbers are still there — kept and labelled, not cleared or passed off as
    // freshly loaded.
    expectStillVisible('$16,100.00')
  })

  it('picks up new values on a successful refresh', async () => {
    stubLoads(jsonResponse(DEMO), jsonResponse({ ...DEMO, total_value: '17000.00' }))

    render(<PortfolioPage />)
    await expectVisible('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    await expectVisible('$17,000.00')
    expect(screen.queryByText(/Showing previously loaded values/)).toBeNull()
  })

  it('shows cash and totals for a portfolio with no holdings', async () => {
    stubLoads(
      jsonResponse({
        ...DEMO,
        holdings: [],
        holdings_value: '0.00',
        total_value: '10000.00',
        cash_allocation_percent: '100.00',
      }),
    )

    render(<PortfolioPage />)

    expect(await screen.findByText('No stock holdings yet')).toBeTruthy()
    expect(screen.getAllByText('$10,000.00').length).toBeGreaterThan(0)
    expect(screen.getAllByText('100.00%').length).toBeGreaterThan(0)
  })

  it('explains a zero-value portfolio instead of drawing an empty bar', async () => {
    stubLoads(
      jsonResponse({
        ...DEMO,
        cash_balance: '0.00',
        holdings: [],
        holdings_value: '0.00',
        total_value: '0.00',
        cash_allocation_percent: null,
      }),
    )

    render(<PortfolioPage />)

    expect(
      await screen.findByText('Allocation unavailable for a zero-value portfolio.'),
    ).toBeTruthy()
  })

  it('does not send the request twice for one refresh', async () => {
    // One load is a sync plus a read, so two refreshes are four calls and no more.
    const mock = stubLoads(jsonResponse(DEMO), jsonResponse(DEMO))

    render(<PortfolioPage />)
    await expectVisible('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await expectVisible('$16,100.00')

    expect(mock).toHaveBeenCalledTimes(4)
  })

  it('says the figures are demo prices until a sync succeeds', async () => {
    stubLoads(jsonResponse(DEMO))

    render(<PortfolioPage />)
    await expectVisible('$16,100.00')

    expect(screen.getByText('Demo prices')).toBeTruthy()
    expect(screen.getByText('Based on demo prices')).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: 'Demo Price' })).toBeTruthy()
  })

  it('names Alpaca Paper and the sync time once the portfolio is synchronised', async () => {
    stubLoads(jsonResponse(SYNCED))

    render(<PortfolioPage />)
    await expectVisible('$9,374.50')

    expect(screen.getByText(/Alpaca Paper/)).toBeTruthy()
    expect(screen.getByText(/Last synced/)).toBeTruthy()
    // Not "Demo Price": the column is the broker's price now.
    expect(screen.getByRole('columnheader', { name: 'Price' })).toBeTruthy()
    expect(screen.queryByText('Demo prices')).toBeNull()

    // And the figures are the broker's, not the demo table's.
    const apple = screen.getByRole('row', { name: /AAPL/ })
    expect(within(apple).getByText('$201.25')).toBeTruthy()
    expect(within(apple).getByText('$1,006.25')).toBeTruthy()
  })

  it('says so when a linked portfolio has not been read from the broker yet', async () => {
    stubLoads(jsonResponse({ ...SYNCED, last_synced_at: null }))

    render(<PortfolioPage />)
    await expectVisible('$9,374.50')

    expect(screen.getByText(/Not synced yet/)).toBeTruthy()
  })
})

/** The half of the page that is about time rather than data.
 *
 * Fake timers throughout, because "every thirty seconds" is not something a test can wait
 * for. Every call count below is a pair: one sync and one read per refresh.
 */
describe('PortfolioPage refresh lifecycle', () => {
  /** Let the pending promise chain in `load` settle without letting any timer fire. */
  async function settle() {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
  }

  it('syncs again thirty seconds later, and again thirty seconds after that', async () => {
    vi.useFakeTimers()
    const mock = stubLoads(
      jsonResponse(DEMO),
      jsonResponse(DEMO),
      jsonResponse(DEMO),
    )

    render(<PortfolioPage />)
    await settle()
    expect(mock).toHaveBeenCalledTimes(2)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(mock).toHaveBeenCalledTimes(4)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(mock).toHaveBeenCalledTimes(6)
  })

  it('stops polling when the page is no longer the one being shown', async () => {
    vi.useFakeTimers()
    const mock = stubLoads(jsonResponse(DEMO), jsonResponse(DEMO))

    const { rerender } = render(<PortfolioPage active />)
    await settle()
    expect(mock).toHaveBeenCalledTimes(2)

    rerender(<PortfolioPage active={false} />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })

    expect(mock).toHaveBeenCalledTimes(2)
  })

  it('refreshes immediately on coming back to the page, rather than waiting for a tick', async () => {
    vi.useFakeTimers()
    const mock = stubLoads(jsonResponse(DEMO), jsonResponse(DEMO))

    const { rerender } = render(<PortfolioPage active={false} />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    // Nothing at all while it is hidden: no request, no timer.
    expect(mock).not.toHaveBeenCalled()

    rerender(<PortfolioPage active />)
    await settle()

    expect(mock).toHaveBeenCalledTimes(2)
  })

  it('clears its timer on unmount', async () => {
    vi.useFakeTimers()
    const mock = stubLoads(jsonResponse(DEMO))

    const { unmount } = render(<PortfolioPage />)
    await settle()
    expect(mock).toHaveBeenCalledTimes(2)

    unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })

    expect(mock).toHaveBeenCalledTimes(2)
  })

  it('does not start a second request while one is still running', async () => {
    // A sync that never settles: the page is mid-refresh for the whole test.
    const mock = vi.fn(() => new Promise<Response>(() => {}))
    vi.stubGlobal('fetch', mock)

    render(<PortfolioPage />)

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    expect(mock).toHaveBeenCalledTimes(1)
  })

  it('keeps the figures and says why when the sync fails', async () => {
    const mock = vi.fn()
    // A first load that succeeds...
    mock.mockResolvedValueOnce(jsonResponse(SYNC_OK))
    mock.mockResolvedValueOnce(jsonResponse(DEMO))
    // ...and a refresh whose sync fails while the read still works, which is the state
    // the backend leaves behind: the previous snapshot, unchanged and readable.
    mock.mockResolvedValueOnce(jsonResponse({ detail: 'Alpaca refused the credentials.' }, 503))
    mock.mockResolvedValueOnce(jsonResponse(DEMO))
    vi.stubGlobal('fetch', mock)

    render(<PortfolioPage />)
    await expectVisible('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    expect(await screen.findByText(/Could not sync/)).toBeTruthy()
    expect(screen.getByText(/Alpaca refused the credentials/)).toBeTruthy()
    // The figures are still on screen, and the page has not claimed they are new.
    expectStillVisible('$16,100.00')
    expect(screen.queryByText(/Showing previously loaded values/)).toBeNull()
  })
})
