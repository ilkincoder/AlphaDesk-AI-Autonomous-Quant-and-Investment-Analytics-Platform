/** Portfolio page behaviour, driven entirely by stubbed responses.
 *
 * Nothing here touches the real API or the seeded demo portfolio: every case is a
 * fixture handed to a stubbed `fetch`, which is the only way to reach the failure,
 * empty, and zero-value states without editing persistent data.
 */

import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Valuation } from '../api'
import { PortfolioPage } from './PortfolioPage'

/** Byte-for-byte the shape `GET /portfolio/valuation` returns for the demo portfolio.
 *  A fixture to test against — the application itself never contains these numbers. */
const DEMO: Valuation = {
  portfolio_id: 1,
  currency: 'USD',
  price_source: 'demo',
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

/** Only the parts of Response the api module reads. Cheaper and more obvious than
 *  building a real Response, which jsdom does not provide. */
function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

function stubFetch(...responses: Response[]) {
  const mock = vi.fn()
  for (const response of responses) mock.mockResolvedValueOnce(response)
  vi.stubGlobal('fetch', mock)
  return mock
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
})

describe('PortfolioPage', () => {
  it('shows card-shaped placeholders before any data arrives', () => {
    // A request that never settles: the page stays in its loading phase.
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))

    render(<PortfolioPage />)

    expect(screen.getByText('Loading portfolio data')).toBeTruthy()
    expect(screen.queryByText('$16,100.00')).toBeNull()
  })

  it('renders every figure from the API response', async () => {
    stubFetch(jsonResponse(DEMO))

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
    stubFetch(jsonResponse(DEMO))

    render(<PortfolioPage />)
    await expectVisible('$16,100.00')

    const cash = screen.getByRole('row', { name: /CASH/ })
    expect(within(cash).getByText('$10,000.00')).toBeTruthy()
    expect(within(cash).getByText('62.11%')).toBeTruthy()
    // Shares and Demo Price are absent for cash, not zero.
    expect(within(cash).getAllByText('—').length).toBe(2)
  })

  it('reports a missing portfolio as "No portfolio available."', async () => {
    stubFetch(
      jsonResponse({ detail: "Portfolio 'AlphaDesk Demo' has not been seeded." }, 404),
    )

    render(<PortfolioPage />)

    expect(await screen.findByText('No portfolio available.')).toBeTruthy()
    // The backend's own explanation is shown rather than replaced.
    expect(screen.getByText(/has not been seeded/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })

  it('surfaces a server error message and recovers when Retry succeeds', async () => {
    stubFetch(
      jsonResponse({ detail: 'No demo price available for held symbol(s): XYZ.' }, 503),
      jsonResponse(DEMO),
    )

    render(<PortfolioPage />)

    expect(await screen.findByText(/No demo price available/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await expectVisible('$16,100.00')
  })

  it('keeps previous values on screen and labels them when a refresh fails', async () => {
    stubFetch(jsonResponse(DEMO), jsonResponse({ detail: 'backend unavailable' }, 503))

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
    stubFetch(
      jsonResponse(DEMO),
      jsonResponse({ ...DEMO, total_value: '17000.00' }),
    )

    render(<PortfolioPage />)
    await expectVisible('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    await expectVisible('$17,000.00')
    expect(screen.queryByText(/Showing previously loaded values/)).toBeNull()
  })

  it('shows cash and totals for a portfolio with no holdings', async () => {
    stubFetch(
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
    stubFetch(
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
    const mock = stubFetch(jsonResponse(DEMO), jsonResponse(DEMO))

    render(<PortfolioPage />)
    await expectVisible('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await expectVisible('$16,100.00')

    expect(mock).toHaveBeenCalledTimes(2)
  })
})
