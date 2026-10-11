/** The Dashboard: what it maps from the four sources, and how it behaves when one fails.
 *
 * The fixtures are the shapes the API really returns -- decimal strings, ISO timestamps, a
 * `null` where there is nothing to report -- and every one of them is supplied by a mock. None
 * of them is a fallback the page falls back to: with no route stubbed the page has nothing to
 * render, which is the property that keeps a fixture from quietly becoming application data.
 *
 * Cards are found by their heading rather than by their text, because a label like "Cash" is
 * also a segment in the allocation legend. That is not a test convenience: two elements reading
 * "Cash" is a page whose parts are only distinguishable by position, and the heading is the
 * thing that distinguishes them.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { aBacktestRun, aBatch, anOrder, jsonResponse, on, routeFetch } from '../testing'
import type { Route } from '../testing'
import { DashboardPage } from './DashboardPage'

/** A valuation, as `GET /portfolio/valuation` returns one. */
function aValuation(overrides: Record<string, unknown> = {}) {
  return {
    portfolio_id: 1,
    currency: 'USD',
    price_source: 'alpaca_paper',
    last_synced_at: '2026-09-28T15:45:19+00:00',
    cash_balance: '1000.00',
    holdings_value: '6100.00',
    total_value: '7100.00',
    cash_allocation_percent: '14.08',
    holdings: [
      {
        symbol: 'AAPL',
        quantity: '8.000000',
        price: '336.64',
        holding_value: '2693.12',
        allocation_percent: '37.93',
      },
      {
        symbol: 'MSFT',
        quantity: '7.000000',
        price: '486.70',
        holding_value: '3406.88',
        allocation_percent: '47.99',
      },
    ],
    ...overrides,
  }
}

function anOrdersView(overrides: Record<string, unknown> = {}) {
  return { active_batch_id: null, batches: [], ...overrides }
}

function aLatestAnalysis(overrides: Record<string, unknown> = {}) {
  return {
    turn_id: 'f'.repeat(32),
    status: 'completed',
    created_at: '2026-09-19T19:08:13+00:00',
    completed_at: '2026-09-19T19:08:35+00:00',
    run_id: 'run-1',
    symbol: 'NVDA',
    start_date: '2026-06-22',
    end_date: '2026-09-19',
    as_of: '2026-09-19',
    destination: 'module1_analysis',
    question: "Compare NVDA's price movement with insider activity.",
    ...overrides,
  }
}

/** The section a heading belongs to, so a card can be asserted about on its own. */
function card(label: string): HTMLElement {
  return screen.getByRole('heading', { name: label }).closest('section') as HTMLElement
}

/** Render the page with every source answered, and wait for the first paint to finish. */
async function open(routes: Route[] = []) {
  const fetchMock = routeFetch([
    ...routes,
    on('GET', '/api/portfolio/valuation', () => jsonResponse(aValuation())),
    on('GET', '/api/orders', () => jsonResponse(anOrdersView())),
    on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: null })),
    on('GET', '/api/analysis/latest', () => jsonResponse({ analysis: null })),
  ])
  vi.stubGlobal('fetch', fetchMock)
  render(<DashboardPage onNavigate={vi.fn()} />)
  await waitFor(() => expect(screen.queryByText('Loading…')).toBeNull())
  return fetchMock
}

beforeEach(() => {
  vi.unstubAllGlobals()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('DashboardPage summary cards', () => {
  it('shows the portfolio value and the cash balance from the valuation', async () => {
    await open()

    const value = card('Portfolio value')
    expect(within(value).getByText('$7,100.00')).toBeTruthy()
    expect(within(value).getByText(/alpaca_paper — paper trading/)).toBeTruthy()
    expect(within(value).getByText(/Last synced/)).toBeTruthy()

    const cash = card('Cash')
    expect(within(cash).getByText('$1,000.00')).toBeTruthy()
    // Cash is named as cash, not as buying power.
    expect(within(cash).getByText(/not broker buying power/)).toBeTruthy()
  })

  it('calls the demo price table fictional rather than presenting it as market data', async () => {
    await open([
      on('GET', '/api/portfolio/valuation', () =>
        jsonResponse(aValuation({ price_source: 'demo', last_synced_at: null })),
      ),
    ])

    const value = card('Portfolio value')
    expect(within(value).getByText(/Fictional demo prices/)).toBeTruthy()
    expect(within(value).getByText(/Never synced from a broker/)).toBeTruthy()
  })

  it('counts the working batches, not the finished ones', async () => {
    await open([
      on('GET', '/api/orders', () =>
        jsonResponse(
          anOrdersView({
            batches: [
              aBatch({ batch_id: 'b1', status: 'completed' }),
              aBatch({ batch_id: 'b2', status: 'partial' }),
              aBatch({ batch_id: 'b3', status: 'submitting' }),
            ],
          }),
        ),
      ),
    ])

    const batches = card('Active order batches')
    expect(within(batches).getByText('1')).toBeTruthy()
    expect(within(batches).getByText(/Still working, out of 3 recorded/)).toBeTruthy()
  })

  it('shows a confirmed zero when no batch is working', async () => {
    await open([
      on('GET', '/api/orders', () =>
        jsonResponse(anOrdersView({ batches: [aBatch({ status: 'completed' })] })),
      ),
    ])

    const batches = card('Active order batches')
    // Zero, because the source answered and the answer is none -- not a dash.
    expect(within(batches).getByText('0')).toBeTruthy()
    expect(within(batches).getByText(/1 recorded in total/)).toBeTruthy()
  })

  it('shows the latest backtest return with its instrument, strategy and a Simulated badge', async () => {
    await open([
      on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: aBacktestRun() })),
    ])

    const latest = card('Latest backtest return')
    expect(within(latest).getByText('-28.56%')).toBeTruthy()
    expect(within(latest).getByText(/NVDA · Moving Average Crossover v1/)).toBeTruthy()
    // Every backtest figure on this page is labelled a simulation.
    expect(within(latest).getByText('Simulated')).toBeTruthy()
    expect(within(card('Latest backtest')).getByText('Simulated')).toBeTruthy()
  })
})

describe('DashboardPage allocation', () => {
  it('renders the donut legend with money and shares, cash included', async () => {
    await open()

    const allocation = card('Allocation')
    // Money, from the valuation's own holding values and cash balance.
    expect(within(allocation).getByText('$2,693.12')).toBeTruthy()
    expect(within(allocation).getByText('$3,406.88')).toBeTruthy()
    expect(within(allocation).getByText('$1,000.00')).toBeTruthy()
    // And the shares beside them.
    expect(within(allocation).getByText('37.93%')).toBeTruthy()
    expect(within(allocation).getByText('14.08%')).toBeTruthy()
    expect(within(allocation).getByText(/Total \$7,100\.00/)).toBeTruthy()
  })

  it('lists the amounts instead of drawing a ring when the portfolio holds a negative balance', async () => {
    await open([
      on('GET', '/api/portfolio/valuation', () =>
        jsonResponse(aValuation({ cash_balance: '-250.00', cash_allocation_percent: null })),
      ),
    ])

    const allocation = card('Allocation')
    // The real amounts, unclamped, and no percentage of a total that has a negative part in it.
    expect(within(allocation).getByText('-$250.00')).toBeTruthy()
    expect(
      within(allocation).getByText(/negative balance in the portfolio makes a percentage/),
    ).toBeTruthy()
    expect(allocation.querySelector('svg')).toBeNull()
  })
})

describe('DashboardPage panels', () => {
  it('summarises the latest analysis from the fields that exist, and claims no score', async () => {
    await open([
      on('GET', '/api/analysis/latest', () => jsonResponse({ analysis: aLatestAnalysis() })),
    ])

    const panel = card('Insider analysis')
    expect(within(panel).getByText('NVDA')).toBeTruthy()
    expect(within(panel).getByText(/Price movement against reported insider activity/)).toBeTruthy()
    expect(within(panel).getByText('completed')).toBeTruthy()
    expect(within(panel).getByText('2026-06-22 to 2026-09-19')).toBeTruthy()
    // Said outright, because this system has no such number and a summary that stayed silent
    // would let a reader assume one.
    expect(within(panel).getByText(/reports no signal score or confidence/)).toBeTruthy()
  })

  it('lists recent paper batches with their status and order counts', async () => {
    await open([
      on('GET', '/api/orders', () =>
        jsonResponse(
          anOrdersView({
            batches: [
              aBatch({ status: 'completed', orders: [anOrder(), anOrder({ sequence: 2 })] }),
            ],
          }),
        ),
      ),
    ])

    const panel = card('Paper orders')
    expect(within(panel).getByText(/Sent to the Alpaca paper account/)).toBeTruthy()
    expect(within(panel).getByText('completed')).toBeTruthy()
    expect(within(panel).getByText('2 orders, 2 filled')).toBeTruthy()
  })

  it('shows the explanation belonging to the same run as the chart', async () => {
    await open([
      on('GET', '/api/backtest/runs/latest', () =>
        jsonResponse({
          run: aBacktestRun({
            symbol: 'AAPL',
            explanation: 'The strategy acted twice over the window and ended flat.',
            explanation_model: 'deepseek-flash',
          }),
        }),
      ),
    ])

    const chart = card('Latest backtest')
    const explanation = card('Backtest risk explanation')

    // One run, read once: the chart's instrument and the explanation's provenance are the same
    // run's, so the two panels cannot describe different tests.
    expect(within(chart).getByText(/AAPL/)).toBeTruthy()
    expect(within(explanation).getByText(/The strategy acted twice over the window/)).toBeTruthy()
    expect(within(explanation).getByText(/deepseek-flash/)).toBeTruthy()
  })

  it('says there is no saved explanation for the run rather than writing one', async () => {
    const fetchMock = await open([
      on('GET', '/api/backtest/runs/latest', () =>
        jsonResponse({ run: aBacktestRun({ explanation: null }) }),
      ),
    ])

    const panel = card('Backtest risk explanation')
    expect(within(panel).getByText('No explanation saved for this run.')).toBeTruthy()
    expect(within(panel).getByRole('button', { name: 'Open backtest to explain' })).toBeTruthy()
    // Nothing was asked of a model: the Dashboard reads explanations, it does not write them.
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).includes('/explanation')),
    ).toHaveLength(0)
  })
})

describe('DashboardPage empty states', () => {
  it('invites a backtest when none has been saved, without running one', async () => {
    const fetchMock = await open()

    expect(screen.getByText(/No backtest has been saved yet/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open backtest' })).toBeTruthy()
    // Still only reads: a dashboard that ran a backtest to fill a card would spend a provider
    // credit to decorate itself.
    expect(fetchMock.mock.calls.every(([, init]) => (init?.method ?? 'GET') === 'GET')).toBe(true)
  })

  it('invites an analysis when none has been completed', async () => {
    await open()

    const panel = card('Insider analysis')
    expect(within(panel).getByText(/No analysis has been completed yet/)).toBeTruthy()
    expect(within(panel).getByRole('button', { name: 'Open Analysis' })).toBeTruthy()
  })

  it('says the portfolio is entirely cash when nothing is held', async () => {
    await open([
      on('GET', '/api/portfolio/valuation', () =>
        jsonResponse(
          aValuation({
            holdings: [],
            holdings_value: '0.00',
            total_value: '1000.00',
            cash_allocation_percent: '100.00',
          }),
        ),
      ),
    ])

    expect(screen.getByText(/the portfolio is entirely cash/)).toBeTruthy()
  })

  it('says so when no orders have been submitted', async () => {
    await open()

    const panel = card('Paper orders')
    expect(within(panel).getByText(/No orders have been submitted/)).toBeTruthy()
  })
})

describe('DashboardPage partial failure', () => {
  it('shows a dash and a retry for the source that failed, and the others in full', async () => {
    await open([
      on('GET', '/api/portfolio/valuation', () =>
        jsonResponse({ detail: 'the price provider is down' }, 503),
      ),
    ])

    const value = card('Portfolio value')
    expect(within(value).getByText('—')).toBeTruthy()
    expect(within(value).getByText(/could not be read/)).toBeTruthy()
    expect(within(value).getByRole('button', { name: 'Retry' })).toBeTruthy()

    // The three sources that answered are untouched by the one that did not.
    expect(screen.getByRole('heading', { name: 'Active order batches' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Latest backtest return' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Paper orders' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Insider analysis' })).toBeTruthy()
  })

  it('keeps the values it already had when a refresh fails, and says they are stale', async () => {
    let failNext = false
    const fetchMock = routeFetch([
      on('GET', '/api/portfolio/valuation', () =>
        failNext
          ? jsonResponse({ detail: 'the price provider is down' }, 503)
          : jsonResponse(aValuation()),
      ),
      on('GET', '/api/orders', () => jsonResponse(anOrdersView())),
      on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: null })),
      on('GET', '/api/analysis/latest', () => jsonResponse({ analysis: null })),
    ])
    vi.stubGlobal('fetch', fetchMock)
    render(<DashboardPage onNavigate={vi.fn()} />)
    await screen.findByText('$7,100.00')

    failNext = true
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    // The figure stays, and the failure is reported once, naming the source it belongs to.
    expect(await screen.findByText(/could not be refreshed/)).toBeTruthy()
    expect(screen.getByText(/the portfolio valuation \(the price provider is down\)/)).toBeTruthy()
    expect(screen.getByText('$7,100.00')).toBeTruthy()
  })

  it('drops a second refresh while one is still running', async () => {
    let release!: (response: Response) => void
    const pending = new Promise<Response>((resolve) => {
      release = resolve
    })
    const valuation = vi.fn(() => pending)
    const fetchMock = routeFetch([
      { match: (url) => url.includes('/api/portfolio/valuation'), respond: valuation },
      on('GET', '/api/orders', () => jsonResponse(anOrdersView())),
      on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: null })),
      on('GET', '/api/analysis/latest', () => jsonResponse({ analysis: null })),
    ])
    vi.stubGlobal('fetch', fetchMock)
    render(<DashboardPage onNavigate={vi.fn()} />)

    const refresh = screen.getByRole('button', { name: /Refres/ })
    fireEvent.click(refresh)
    fireEvent.click(refresh)
    fireEvent.click(refresh)

    expect(valuation).toHaveBeenCalledTimes(1)
    release(jsonResponse(aValuation()))
    await screen.findByText('$7,100.00')
  })
})

describe('DashboardPage reading and navigating', () => {
  it('only ever reads: no POST, no order, no proposal, no run, no model call', async () => {
    const fetchMock = await open([
      on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: aBacktestRun() })),
      on('GET', '/api/analysis/latest', () => jsonResponse({ analysis: aLatestAnalysis() })),
    ])

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]) => String(url).includes('/api/portfolio/valuation')),
      ).toHaveLength(2),
    )

    const methods = new Set(fetchMock.mock.calls.map(([, init]) => init?.method ?? 'GET'))
    expect([...methods]).toEqual(['GET'])
  })

  it('navigates to the pages the panels point at', async () => {
    const onNavigate = vi.fn()
    const fetchMock = routeFetch([
      on('GET', '/api/portfolio/valuation', () => jsonResponse(aValuation())),
      on('GET', '/api/orders', () => jsonResponse(anOrdersView())),
      on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: aBacktestRun() })),
      on('GET', '/api/analysis/latest', () => jsonResponse({ analysis: aLatestAnalysis() })),
    ])
    vi.stubGlobal('fetch', fetchMock)
    render(<DashboardPage onNavigate={onNavigate} />)
    await screen.findByText('$7,100.00')

    // One control per destination, so each of these is unambiguous by its name alone.
    fireEvent.click(screen.getByRole('button', { name: 'Open portfolio' }))
    expect(onNavigate).toHaveBeenCalledWith('portfolio')

    fireEvent.click(screen.getByRole('button', { name: 'Open backtest' }))
    expect(onNavigate).toHaveBeenCalledWith('backtest')

    fireEvent.click(screen.getByRole('button', { name: 'View orders' }))
    expect(onNavigate).toHaveBeenCalledWith('orders')

    fireEvent.click(screen.getByRole('button', { name: 'Open Analysis' }))
    expect(onNavigate).toHaveBeenCalledWith('analysis')
  })

  it('opens Orders focused on the batch that a row names', async () => {
    const onOpenOrders = vi.fn()
    const fetchMock = routeFetch([
      on('GET', '/api/portfolio/valuation', () => jsonResponse(aValuation())),
      on('GET', '/api/orders', () =>
        jsonResponse(anOrdersView({ batches: [aBatch({ batch_id: 'batch-9' })] })),
      ),
      on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: null })),
      on('GET', '/api/analysis/latest', () => jsonResponse({ analysis: null })),
    ])
    vi.stubGlobal('fetch', fetchMock)
    render(<DashboardPage onNavigate={vi.fn()} onOpenOrders={onOpenOrders} />)
    await screen.findByText('$7,100.00')

    fireEvent.click(screen.getByRole('button', { name: 'Open' }))

    // The Orders page's own focus behaviour, reached from here rather than reimplemented.
    expect(onOpenOrders).toHaveBeenCalledWith('batch-9')
  })
})
