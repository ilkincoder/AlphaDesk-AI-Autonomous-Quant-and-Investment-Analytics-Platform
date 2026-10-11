/** The Backtest page: both ways of choosing an asset, the two states that page usually gets
 *  wrong (a failed run and a second click), and what a result has to show.
 *
 * The numbers are checked as text rather than recomputed here. The page does no financial
 * arithmetic -- `test_backtest_engine.py` is where the arithmetic is proved -- so what this file
 * proves is that the API's own figures reach the screen unchanged, and that the page never
 * invents one of its own.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  aBacktestRun,
  anInstrument,
  anRsiStrategy,
  aStrategy,
  jsonResponse,
  on,
  routeFetch,
} from '../testing'
import type { Route } from '../testing'
import { BacktestPage } from './BacktestPage'

const HOLDINGS = {
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
      symbol: 'NVDA',
      quantity: '10.000000',
      price: '150.00',
      holding_value: '1500.00',
      allocation_percent: '9.32',
    },
  ],
}

function strategiesBody(strategies = [aStrategy()]) {
  return { strategies }
}

/** Render the page with a stubbed API, and wait for the catalogue to arrive.
 *
 * `routes` are consulted first, so a test overrides one endpoint by naming it and every other
 * endpoint keeps its working stub. Naming the portfolio route, for instance, must not also take
 * away the catalogue -- a page with no strategies cannot reach the state under test.
 */
async function openPage(routes: Route[] = []) {
  const fetchMock = routeFetch([
    ...routes,
    on('GET', '/api/backtest/strategies', () => jsonResponse(strategiesBody())),
    // The portfolio tab loads its holdings as soon as it is shown.
    on('GET', '/api/portfolio/valuation', () => jsonResponse(HOLDINGS)),
    // Nothing saved, which is how every test but the restoring ones should start. A test that
    // wants a saved run names this route first and this stub stands down.
    on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: null })),
  ])
  vi.stubGlobal('fetch', fetchMock)
  render(<BacktestPage />)
  await screen.findByRole('button', { name: /Moving Average Crossover/ })
  return fetchMock
}

/** Choose the strategy and a holding, which is the shortest path to a runnable form. */
async function pickStrategyAndHolding() {
  fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))
  const select = await screen.findByLabelText('Holding')
  fireEvent.change(select, { target: { value: 'NVDA' } })
}

/** The body of the POST to `fragment`.
 *
 * The method is part of the match, not just the URL. `/api/backtest/runs` is a *prefix* of
 * `/api/backtest/runs/latest`, so a fragment match alone would find the restore read that the
 * page makes on load -- which carries no body -- and quietly return `{}` instead of the run.
 */
function bodyOf(fetchMock: ReturnType<typeof vi.fn>, fragment: string) {
  const call = fetchMock.mock.calls.find(
    ([url, init]) => String(url).includes(fragment) && init?.method === 'POST',
  )
  if (!call) throw new Error(`no POST request to ${fragment}`)
  return JSON.parse(String(call[1]?.body ?? '{}'))
}

beforeEach(() => {
  vi.unstubAllGlobals()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('BacktestPage strategy catalogue', () => {
  it('offers the predefined strategy with its explanation and default periods', async () => {
    await openPage()

    const card = screen.getByRole('button', { name: /Moving Average Crossover/ })
    expect(card.textContent).toContain('simple moving average')
    // The defaults are visible on the card as well as in the form.
    expect(card.textContent).toContain('20/50')
    expect(card.getAttribute('aria-pressed')).toBe('false')
  })

  it('marks the chosen strategy and reveals the asset choices', async () => {
    await openPage()

    expect(screen.queryByLabelText('Holding')).toBeNull()
    expect(screen.getByText('Choose a strategy first.')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))

    expect(
      screen.getByRole('button', { name: /Moving Average Crossover/ }).getAttribute('aria-pressed'),
    ).toBe('true')
    expect(await screen.findByLabelText('Holding')).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'From my portfolio' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Search for another stock' })).toBeTruthy()
  })

  it('reports a catalogue that could not be loaded, with a retry', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse({ detail: 'The API returned 503.' }, 503)),
    )

    render(<BacktestPage />)

    expect(await screen.findByText('Could not load the strategies.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })
})

describe('BacktestPage asset selection', () => {
  it('lists the holdings and says what choosing one does not do', async () => {
    await openPage()
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))

    const select = await screen.findByLabelText('Holding')
    const options = within(select).getAllByRole('option').map((option) => option.textContent)

    // The stocks the portfolio holds, and cash is not among them: it is not a stock and has no
    // price series to test an average against.
    expect(options).toEqual(['Choose a holding', 'AAPL', 'NVDA'])
    expect(screen.getByText(/Its quantity and purchase price are not used/)).toBeTruthy()
  })

  it('says so plainly when the portfolio holds no stock', async () => {
    await openPage([
      on('GET', '/api/portfolio/valuation', () =>
        jsonResponse({ ...HOLDINGS, holdings: [], holdings_value: '0.00' }),
      ),
    ])
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))

    expect(
      await screen.findByText('This portfolio holds no stock. Search for another stock instead.'),
    ).toBeTruthy()
  })

  it('reports an unreadable portfolio without breaking the search path', async () => {
    await openPage([
      on('GET', '/api/portfolio/valuation', () => jsonResponse({ detail: 'No portfolio.' }, 404)),
    ])
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))

    expect(await screen.findByText('No portfolio is stored. Search for a stock instead.')).toBeTruthy()

    fireEvent.click(screen.getByRole('tab', { name: 'Search for another stock' }))
    expect(screen.getByLabelText('Enter a ticker symbol or company name.')).toBeTruthy()
  })

  it('shows search results with the ticker, the company name and the exchange', async () => {
    const fetchMock = await openPage([
      on('GET', '/api/backtest/instruments', () =>
        jsonResponse({
          query: 'nvidia',
          matches: [anInstrument(), anInstrument({ symbol: 'NVDA', exchange: 'IEX' })],
        }),
      ),
    ])
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))
    fireEvent.click(screen.getByRole('tab', { name: 'Search for another stock' }))

    fireEvent.change(screen.getByLabelText('Enter a ticker symbol or company name.'), {
      target: { value: 'nvidia' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    const results = await screen.findByRole('list', { name: 'Search results' })
    expect(within(results).getAllByRole('listitem')).toHaveLength(2)
    expect(results.textContent).toContain('NVDA')
    expect(results.textContent).toContain('NVIDIA Corporation')
    expect(results.textContent).toContain('NASDAQ')
    expect(results.textContent).toContain('IEX')

    // A result is offered, not assumed: the run is not runnable until one is chosen.
    expect(screen.getByText('Choose one of these to continue.')).toBeTruthy()
    const searched = fetchMock.mock.calls.map(([url]) => String(url)).find((url) =>
      url.includes('/api/backtest/instruments'),
    )
    expect(searched).toContain('q=nvidia')
  })

  it('resolves the chosen result, exchange and all', async () => {
    await openPage([
      on('GET', '/api/backtest/instruments', () =>
        jsonResponse({ query: 'nvda', matches: [anInstrument()] }),
      ),
    ])
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))
    fireEvent.click(screen.getByRole('tab', { name: 'Search for another stock' }))
    fireEvent.change(screen.getByLabelText('Enter a ticker symbol or company name.'), {
      target: { value: 'nvda' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    fireEvent.click(await screen.findByRole('button', { name: /NVDA/ }))

    expect(screen.getByText('Selected: NVDA · NASDAQ')).toBeTruthy()
    expect(screen.getByRole('button', { name: /NVDA/ }).getAttribute('aria-pressed')).toBe('true')
  })

  it('says that nothing matched rather than showing an empty list', async () => {
    await openPage([
      on('GET', '/api/backtest/instruments', () =>
        jsonResponse({ query: 'zzzzqqqq', matches: [] }),
      ),
    ])
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))
    fireEvent.click(screen.getByRole('tab', { name: 'Search for another stock' }))
    fireEvent.change(screen.getByLabelText('Enter a ticker symbol or company name.'), {
      target: { value: 'zzzzqqqq' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText(/No US-listed stock matches/)).toBeTruthy()
  })

  it('reports a search that failed differently from one that found nothing', async () => {
    await openPage([
      on('GET', '/api/backtest/instruments', () =>
        jsonResponse({ detail: 'Twelve Data could not be reached.' }, 502),
      ),
    ])
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))
    fireEvent.click(screen.getByRole('tab', { name: 'Search for another stock' }))
    fireEvent.change(screen.getByLabelText('Enter a ticker symbol or company name.'), {
      target: { value: 'nvda' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    expect(await screen.findByText('Twelve Data could not be reached.')).toBeTruthy()
  })
})

describe('BacktestPage running a test', () => {
  it('sends the configuration the form holds, and no quantity or price from the portfolio', async () => {
    const fetchMock = await openPage([
      on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun())),
    ])
    await pickStrategyAndHolding()

    fireEvent.change(screen.getByLabelText('Initial capital (USD)'), {
      target: { value: '2500.00' },
    })
    fireEvent.change(screen.getByLabelText('Fast period (sessions)'), { target: { value: '10' } })
    fireEvent.change(screen.getByLabelText('Slow period (sessions)'), { target: { value: '30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    await screen.findByText('Net total return')

    const body = bodyOf(fetchMock, '/api/backtest/runs')
    expect(body.symbol).toBe('NVDA')
    expect(body.strategy_id).toBe('moving_average_crossover')
    expect(body.initial_capital).toBe('2500.00')
    expect(body.parameters).toEqual({ fast_period: 10, slow_period: 30 })
    expect(body.costs).toEqual({ commission_per_share: '0.005', slippage_percent: '0.05' })
    expect(typeof body.request_id).toBe('string')
    // The holding's exchange is unknown to this page, so it is not sent -- and neither is the
    // holding's quantity or its purchase price, which the form never carried.
    expect(body.exchange).toBeUndefined()
    expect(body.quantity).toBeUndefined()
  })

  it('sends the chosen exchange when the asset came from a search', async () => {
    const fetchMock = await openPage([
      on('GET', '/api/backtest/instruments', () =>
        jsonResponse({ query: 'nvda', matches: [anInstrument()] }),
      ),
      on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun())),
    ])
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))
    fireEvent.click(screen.getByRole('tab', { name: 'Search for another stock' }))
    fireEvent.change(screen.getByLabelText('Enter a ticker symbol or company name.'), {
      target: { value: 'nvda' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    fireEvent.click(await screen.findByRole('button', { name: /NVDA/ }))

    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    await screen.findByText('Net total return')

    expect(bodyOf(fetchMock, '/api/backtest/runs').exchange).toBe('NASDAQ')
  })

  it('runs once however many times the button is pressed', async () => {
    let release!: (response: Response) => void
    const pending = new Promise<Response>((done) => {
      release = done
    })
    const posts = vi.fn(() => pending)
    const fetchMock = routeFetch([
      on('GET', '/api/portfolio/valuation', () => jsonResponse(HOLDINGS)),
      on('GET', '/api/backtest/strategies', () => jsonResponse(strategiesBody())),
      // The routes are listed by hand here because the POST below never resolves. The restore
      // read still has to be answered: left unrouted it fails, and the note the page shows for a
      // failed restore is a second `role="status"` beside the one this test is looking for.
      on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: null })),
      { match: (_url: string, init?: RequestInit) => (init?.method ?? 'GET') === 'POST',
        respond: posts },
    ])
    vi.stubGlobal('fetch', fetchMock)

    render(<BacktestPage />)
    await screen.findByRole('button', { name: /Moving Average Crossover/ })
    await pickStrategyAndHolding()

    const run = screen.getByRole('button', { name: 'Run backtest' })
    fireEvent.click(run)
    fireEvent.click(run)
    fireEvent.click(run)

    expect(posts).toHaveBeenCalledTimes(1)
    // And the page says it is working rather than looking idle.
    expect(screen.getByRole('status').textContent).toContain('running the test')

    release(jsonResponse(aBacktestRun()))
    await screen.findByText('Net total return')
  })

  it('lets the strategy refuse a fast period that is not shorter than the slow one', async () => {
    // Whether a fast period must be shorter than a slow one is the *strategy's* rule, and the
    // strategy is where it is stated -- the server refuses it before it asks the provider for
    // anything. This page relays what it is told rather than keeping a second copy of a rule
    // that belongs to a strategy it can be handed a new one of at any time.
    const complaint =
      'the fast period (60) must be shorter than the slow period (50), or the averages would ' +
      'cross on every session'
    const fetchMock = await openPage([
      on('POST', '/api/backtest/runs', () => jsonResponse({ detail: complaint }, 422)),
    ])
    await pickStrategyAndHolding()

    fireEvent.change(screen.getByLabelText('Fast period (sessions)'), { target: { value: '60' } })
    fireEvent.change(screen.getByLabelText('Slow period (sessions)'), { target: { value: '50' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    expect(await screen.findByText(complaint)).toBeTruthy()
    // And the configuration is still on screen, so the number that was wrong can be fixed.
    expect((screen.getByLabelText('Fast period (sessions)') as HTMLInputElement).value).toBe('60')
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  })

  it('refuses a capital that is not a positive amount, and one with too many decimals', async () => {
    const fetchMock = await openPage()
    await pickStrategyAndHolding()

    fireEvent.change(screen.getByLabelText('Initial capital (USD)'), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    expect(await screen.findByText(/greater than zero/)).toBeTruthy()

    // Three decimals would be rounded on the way into NUMERIC(18,2) while the run used the
    // unrounded value, which is a row describing an arithmetic nobody performed.
    fireEvent.change(screen.getByLabelText('Initial capital (USD)'), {
      target: { value: '1000.005' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    expect(await screen.findByText(/two decimal places/)).toBeTruthy()

    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
  })

  it('documents the units and defaults of the advanced settings', async () => {
    await openPage()
    await pickStrategyAndHolding()

    fireEvent.click(screen.getByText('Advanced settings'))

    expect(screen.getByLabelText('Commission (USD per share, each side)')).toBeTruthy()
    expect(screen.getByLabelText('Slippage (percent of price, against the trade)')).toBeTruthy()
    expect(screen.getByText(/Defaults: 0.005 USD per share and 0.05%/)).toBeTruthy()
  })
})

describe('BacktestPage results', () => {
  it('shows the figures the API returned, unchanged', async () => {
    await openPage([on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun()))])
    await pickStrategyAndHolding()
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    await screen.findByText('Net total return')

    // Scoped to the metric itself, because the chart labels its own gridlines in the same
    // currency and one of them is the final value.
    const metric = screen.getByText('Net total return').nextElementSibling
    expect(metric?.textContent).toBe('-28.56%')
    expect(screen.getByText('Final value').nextElementSibling?.textContent).toBe('$714.43')
    expect(screen.getByText('Maximum drawdown').nextElementSibling?.textContent).toBe('37.39%')
    // One completed trade: a buy and the sell that closed it. The two ledger rows are that one
    // round trip, not two trades.
    expect(screen.getByText('Completed trades').nextElementSibling?.textContent).toBe('1')
  })

  it('labels the result a price return and says why', async () => {
    await openPage([on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun()))])
    await pickStrategyAndHolding()
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    expect(await screen.findByText('Price return · simulated')).toBeTruthy()

    fireEvent.click(screen.getByText('How this was calculated, and what it does not tell you'))
    expect(
      screen.getByText(/Prices are split-adjusted and not dividend-adjusted/),
    ).toBeTruthy()
    expect(screen.getByText(/Signals execute at the next session's open/)).toBeTruthy()
  })

  it('puts the strategy and buy-and-hold on one chart, told apart by more than colour', async () => {
    await openPage([on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun()))])
    await pickStrategyAndHolding()
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    const chart = await screen.findByRole('img', { name: /Equity curves/ })
    const lines = chart.querySelectorAll('polyline')
    expect(lines).toHaveLength(2)
    // The benchmark is dashed as well as differently coloured.
    expect(lines[0].getAttribute('stroke-dasharray')).toBeTruthy()
    expect(lines[1].getAttribute('stroke-dasharray')).toBeNull()

    const legend = screen.getByText('Buy and hold')
    expect(legend).toBeTruthy()
    expect(screen.getByText(/Buy and hold, on the same instrument and costs/)).toBeTruthy()
  })

  it('lists every trade with its dates, side, quantity, price and costs', async () => {
    await openPage([on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun()))])
    await pickStrategyAndHolding()
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row')
    // One heading row and two trades.
    expect(rows).toHaveLength(3)

    const header = within(rows[0]).getAllByRole('columnheader').map((cell) => cell.textContent)
    expect(header).toEqual([
      'Executed',
      'Signalled',
      'Side',
      'Quantity',
      'Execution price',
      'Value',
      'Commission',
      'Cash after',
    ])

    const first = within(rows[1]).getAllByRole('cell').map((cell) => cell.textContent)
    // The signal is one session before the execution, which is the rule the engine follows and
    // the one thing a ledger of a backtest is most often wrong about.
    expect(first).toEqual([
      '2024-01-08',
      '2024-01-05',
      'buy',
      '71',
      '$14.0070',
      '$994.50',
      '$0.36',
      '$5.14',
    ])
  })

  it('reports the cash left over and any position still open', async () => {
    const open = aBacktestRun({
      open_position: {
        quantity: 71,
        entry_date: '2024-01-08',
        entry_price: '14.0070',
        mark_date: '2024-01-11',
        mark_price: '12.0000',
        market_value: '852.00',
      },
      cash_remaining: '5.14',
    })
    await openPage([on('POST', '/api/backtest/runs', () => jsonResponse(open))])
    await pickStrategyAndHolding()
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    expect(await screen.findByText(/Cash remaining: \$5.14/)).toBeTruthy()
    expect(screen.getByText(/Holding 71 shares, bought 2024-01-08/)).toBeTruthy()
    expect(screen.getByText(/marked to \$12.0000/)).toBeTruthy()
  })

  it('explains that no trades happened rather than showing an empty table', async () => {
    const quiet = aBacktestRun({
      trades: [],
      equity: [
        { on: '2024-01-05', equity: '1000.00' },
        { on: '2024-01-12', equity: '1000.00' },
      ],
      benchmark_equity: [
        { on: '2024-01-05', equity: '1000.00' },
        { on: '2024-01-12', equity: '1000.00' },
      ],
    })
    await openPage([on('POST', '/api/backtest/runs', () => jsonResponse(quiet))])
    await pickStrategyAndHolding()
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    expect(await screen.findByText(/produced no trades over this period/)).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('keeps the configuration visible when a run fails', async () => {
    await openPage([
      on('POST', '/api/backtest/runs', () =>
        jsonResponse({ detail: 'The fast period (50) must be shorter than the slow period (20).' }, 422),
      ),
    ])
    await pickStrategyAndHolding()

    fireEvent.change(screen.getByLabelText('Initial capital (USD)'), {
      target: { value: '7777.00' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    expect(
      await screen.findByText(
        'The fast period (50) must be shorter than the slow period (20).',
      ),
    ).toBeTruthy()
    expect(screen.getByText(/Your settings are unchanged/)).toBeTruthy()

    // Every field, and the chosen asset, exactly as they were.
    expect((screen.getByLabelText('Initial capital (USD)') as HTMLInputElement).value).toBe(
      '7777.00',
    )
    expect((screen.getByLabelText('Holding') as HTMLSelectElement).value).toBe('NVDA')
    expect(screen.getByRole('button', { name: /Moving Average Crossover/ }).getAttribute('aria-pressed')).toBe('true')
  })

  it('reuses the request id for a retry of the same configuration', async () => {
    const fetchMock = await openPage([
      on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun())),
    ])
    await pickStrategyAndHolding()

    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    await screen.findByText('Net total return')
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST'),
      ).toHaveLength(2),
    )

    const ids = fetchMock.mock.calls
      .filter(([, init]) => init?.method === 'POST')
      .map(([, init]) => JSON.parse(String(init?.body)).request_id)

    // The same id both times, so an accidental resubmit of an unchanged form is answered from
    // the run the server already has instead of fetching the same bars twice.
    expect(ids[0]).toBe(ids[1])
  })

  it('sends a different request id once the configuration changes', async () => {
    const fetchMock = await openPage([
      on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun())),
    ])
    await pickStrategyAndHolding()

    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    await screen.findByText('Net total return')

    fireEvent.change(screen.getByLabelText('Fast period (sessions)'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST'),
      ).toHaveLength(2),
    )

    const ids = fetchMock.mock.calls
      .filter(([, init]) => init?.method === 'POST')
      .map(([, init]) => JSON.parse(String(init?.body)).request_id)

    // A changed configuration is a different question, and the server is entitled to answer it
    // separately rather than being handed a cached answer to the old one.
    expect(ids[0]).not.toBe(ids[1])
  })
})

describe('BacktestPage choosing between strategies', () => {
  async function openTwoStrategies(routes: Route[] = []) {
    return openPage([
      ...routes,
      on('GET', '/api/backtest/strategies', () =>
        jsonResponse(strategiesBody([aStrategy(), anRsiStrategy()])),
      ),
    ])
  }

  it('offers both strategies with the parameters each one declares', async () => {
    await openTwoStrategies()

    expect(screen.getByRole('button', { name: /Moving Average Crossover/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /RSI Reversion/ })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /RSI Reversion/ }))

    // Three fields, because this strategy declares three settings -- and no fast/slow pair,
    // which it does not have.
    expect(screen.getByLabelText('RSI period (sessions)')).toBeTruthy()
    expect(screen.getByLabelText('Entry threshold (buy below this RSI)')).toBeTruthy()
    expect(screen.getByLabelText('Exit threshold (sell above this RSI)')).toBeTruthy()
    expect(screen.queryByLabelText('Fast period (sessions)')).toBeNull()
  })

  it('starts each strategy from its own defaults', async () => {
    await openTwoStrategies()

    fireEvent.click(screen.getByRole('button', { name: /RSI Reversion/ }))

    const value = (label: string) => (screen.getByLabelText(label) as HTMLInputElement).value
    expect(value('RSI period (sessions)')).toBe('14')
    expect(value('Entry threshold (buy below this RSI)')).toBe('30')
    expect(value('Exit threshold (sell above this RSI)')).toBe('70')

    // Switching back restores the crossover's own defaults rather than keeping RSI's numbers.
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))
    expect(value('Fast period (sessions)')).toBe('20')
    expect(value('Slow period (sessions)')).toBe('50')
  })

  it('sends the chosen strategy parameters and nothing else', async () => {
    const fetchMock = await openTwoStrategies([
      on('POST', '/api/backtest/runs', () =>
        jsonResponse(aBacktestRun({ strategy_id: 'rsi_reversion', parameters: { period: 10, entry_threshold: 25, exit_threshold: 75 } })),
      ),
    ])
    fireEvent.click(screen.getByRole('button', { name: /RSI Reversion/ }))
    const select = await screen.findByLabelText('Holding')
    fireEvent.change(select, { target: { value: 'NVDA' } })

    fireEvent.change(screen.getByLabelText('RSI period (sessions)'), { target: { value: '10' } })
    fireEvent.change(screen.getByLabelText('Entry threshold (buy below this RSI)'), {
      target: { value: '25' },
    })
    fireEvent.change(screen.getByLabelText('Exit threshold (sell above this RSI)'), {
      target: { value: '75' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))

    await screen.findByText('Net total return')

    const body = bodyOf(fetchMock, '/api/backtest/runs')
    expect(body.strategy_id).toBe('rsi_reversion')
    // No fast or slow period: a parameter the strategy does not declare is not sent, which is
    // what stops a crossover setting riding along into an RSI run.
    expect(body.parameters).toEqual({ period: 10, entry_threshold: 25, exit_threshold: 75 })
  })

  it('restores a saved RSI run with its own parameters', async () => {
    await openTwoStrategies([
      on('GET', '/api/backtest/runs/latest', () =>
        jsonResponse({
          run: aBacktestRun({
            strategy_id: 'rsi_reversion',
            strategy_name: 'RSI Reversion',
            parameters: { period: 10, entry_threshold: 25, exit_threshold: 75 },
          }),
        }),
      ),
    ])

    await screen.findByText('Net total return')

    const value = (label: string) => (screen.getByLabelText(label) as HTMLInputElement).value
    expect(value('RSI period (sessions)')).toBe('10')
    expect(value('Entry threshold (buy below this RSI)')).toBe('25')
    expect(value('Exit threshold (sell above this RSI)')).toBe('75')
    expect(screen.getByRole('button', { name: /RSI Reversion/ }).ariaPressed).toBe('true')
  })
})

describe('BacktestPage explaining a result', () => {
  const EXPLANATION =
    'The strategy acted twice and ended at 714.43 USD, a return of -28.56%.\n\n' +
    'Buy and hold returned -16.69% over the same window.'

  function anExplanation(overrides = {}) {
    return {
      status: 'ok',
      reason: null,
      detail: null,
      explanation: EXPLANATION,
      model: 'deepseek-flash',
      prompt_version: '1',
      ...overrides,
    }
  }

  /** Run a backtest, so the results panel — and the explanation panel inside it — is on screen. */
  async function runOne(routes: Route[] = []) {
    const fetchMock = await openPage([
      ...routes,
      on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun())),
    ])
    await pickStrategyAndHolding()
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    await screen.findByText('Net total return')
    return fetchMock
  }

  it('offers to explain the result and shows the model’s prose when it arrives', async () => {
    const fetchMock = await runOne([
      on('POST', '/api/backtest/runs/run-1/explanation', () => jsonResponse(anExplanation())),
    ])

    fireEvent.click(screen.getByRole('button', { name: 'Explain results' }))

    expect(await screen.findByText(/acted twice and ended at 714.43/)).toBeTruthy()
    expect(screen.getByText(/Buy and hold returned -16.69%/)).toBeTruthy()
    // Who wrote it, and that it is prose rather than a figure this page calculated.
    expect(screen.getByText(/deepseek-flash, prompt version 1/)).toBeTruthy()
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).includes('/explanation')),
    ).toHaveLength(1)
  })

  it('keeps the results on screen when the explanation cannot be written', async () => {
    await runOne([
      on('POST', '/api/backtest/runs/run-1/explanation', () =>
        jsonResponse({
          status: 'unavailable',
          reason: 'provider_unavailable',
          detail: 'the provider did not answer',
          explanation: null,
          model: null,
          prompt_version: null,
        }),
      ),
    ])

    fireEvent.click(screen.getByRole('button', { name: 'Explain results' }))

    expect(await screen.findByText(/could not be written/)).toBeTruthy()
    // The figures are still there, and the page says why they are unaffected.
    expect(screen.getByText('Net total return')).toBeTruthy()
    expect(screen.getByText(/results above are unaffected/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('does not offer a retry when there is no key to retry with', async () => {
    await runOne([
      on('POST', '/api/backtest/runs/run-1/explanation', () =>
        jsonResponse({
          status: 'unavailable',
          reason: 'model_not_configured',
          detail: 'no DeepSeek API key is configured',
          explanation: null,
          model: null,
          prompt_version: null,
        }),
      ),
    ])

    fireEvent.click(screen.getByRole('button', { name: 'Explain results' }))

    expect(await screen.findByText(/could not be written/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('offers a retry when the request itself failed', async () => {
    let attempts = 0
    await runOne([
      on('POST', '/api/backtest/runs/run-1/explanation', () => {
        attempts += 1
        return attempts === 1
          ? jsonResponse({ detail: 'the API is unreachable' }, 500)
          : jsonResponse(anExplanation())
      }),
    ])

    fireEvent.click(screen.getByRole('button', { name: 'Explain results' }))
    await screen.findByText(/could not be written/)

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByText(/acted twice and ended at 714.43/)).toBeTruthy()
  })

  it('shows a saved explanation on load without asking for it again', async () => {
    // The explanation is stored with the run, so restoring brings it back as it was. Nothing is
    // run and nothing is explained here: this is a page load and nothing else.
    const fetchMock = await openPage([
      on('GET', '/api/backtest/runs/latest', () =>
        jsonResponse({
          run: aBacktestRun({
            explanation: EXPLANATION,
            explanation_model: 'deepseek-flash',
            explanation_prompt_version: '1',
          }),
        }),
      ),
    ])

    expect(await screen.findByText(/acted twice and ended at 714.43/)).toBeTruthy()
    // Nothing was asked for: the prose came back with the run.
    expect(screen.queryByRole('button', { name: 'Explain results' })).toBeNull()
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).includes('/explanation')),
    ).toHaveLength(0)
  })
})

describe('BacktestPage restoring the last run', () => {
  it('puts back the strategy, the instrument, the configuration and the results', async () => {
    await openPage([
      on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: aBacktestRun() })),
    ])

    // The results, without anyone having pressed Run.
    expect((await screen.findByText('Net total return')).nextElementSibling?.textContent).toBe(
      '-28.56%',
    )

    // The configuration the run was made with, not the form's defaults.
    expect(screen.getByRole('button', { name: /Moving Average Crossover/ }).ariaPressed).toBe(
      'true',
    )
    expect((screen.getByLabelText('Start date') as HTMLInputElement).value).toBe('2024-01-05')
    expect((screen.getByLabelText('Initial capital (USD)') as HTMLInputElement).value).toBe(
      '1000.00',
    )
    expect((screen.getByLabelText('Fast period (sessions)') as HTMLInputElement).value).toBe('2')
    expect((screen.getByLabelText('Slow period (sessions)') as HTMLInputElement).value).toBe('3')

    // The instrument is the listing the run resolved, so running it again resolves the same one.
    expect(screen.getByText('NVDA · NASDAQ')).toBeTruthy()
  })

  it('does not run the backtest again or fetch prices to restore', async () => {
    // The stored row already holds the curves, so restoring is one read. Re-running would spend
    // a provider credit to arrive at the same figures.
    const fetchMock = await openPage([
      on('GET', '/api/backtest/runs/latest', () => jsonResponse({ run: aBacktestRun() })),
    ])
    await screen.findByText('Net total return')

    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(posts).toHaveLength(0)
  })

  it('starts from an empty form when nothing has been saved', async () => {
    await openPage()

    // Nothing saved is not a failure, so there is no note about one.
    expect(screen.queryByText(/last saved run could not be loaded/)).toBeNull()
    expect(screen.queryByText('Net total return')).toBeNull()

    // The form is the one the page builds for itself: today's window and the defaults.
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))
    expect((screen.getByLabelText('Initial capital (USD)') as HTMLInputElement).value).toBe(
      '10000.00',
    )
    expect((screen.getByLabelText('Fast period (sessions)') as HTMLInputElement).value).toBe('20')
  })

  it('says a saved run could not be read without refusing the page', async () => {
    await openPage([
      on('GET', '/api/backtest/runs/latest', () =>
        jsonResponse({ detail: 'the database is unreachable' }, 500),
      ),
      on('POST', '/api/backtest/runs', () => jsonResponse(aBacktestRun())),
    ])

    // A note, not an error: the catalogue, the form and a fresh run all still work.
    expect(await screen.findByText(/last saved run could not be loaded/)).toBeTruthy()

    await pickStrategyAndHolding()
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    expect(await screen.findByText('Net total return')).toBeTruthy()
  })

  it('does not overwrite an edit made while the restore was still in flight', async () => {
    // The restore is held open so the person can act first, which is the whole point: reading
    // the network takes time, and a form someone has begun filling in is worth more than a
    // response to a request they made before they touched it.
    let release = () => {}
    const held = new Promise<void>((resolve) => {
      release = resolve
    })

    await openPage([
      on('GET', '/api/backtest/runs/latest', async () => {
        await held
        return jsonResponse({ run: aBacktestRun() })
      }),
    ])

    // Choosing a strategy is what reveals the form, and it is also the first edit.
    fireEvent.click(screen.getByRole('button', { name: /Moving Average Crossover/ }))
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2030-01-01' } })

    // Let the held response arrive and be processed.
    await act(async () => {
      release()
      await new Promise((resolve) => setTimeout(resolve, 0))
    })

    // The whole restore is dropped, not applied around the edit: the window is the one that was
    // typed, the capital is still the default rather than the run's, and no results appeared.
    expect((screen.getByLabelText('Start date') as HTMLInputElement).value).toBe('2030-01-01')
    expect((screen.getByLabelText('Initial capital (USD)') as HTMLInputElement).value).toBe(
      '10000.00',
    )
    expect(screen.queryByText('Net total return')).toBeNull()
  })
})
