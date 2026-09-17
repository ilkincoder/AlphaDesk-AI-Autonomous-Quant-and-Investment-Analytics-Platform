/** The What if? page, driven by stubbed responses.
 *
 * The fixtures are the same response shapes the demo portfolio really returns. Nothing
 * here touches the API or the seeded data.
 */

import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Scenario, Valuation } from '../api'
import { WhatIfPage } from './WhatIfPage'

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

/** NVDA down 10%, exactly as the backend returns it for the demo portfolio. */
const SCENARIO: Scenario = {
  portfolio_id: 1,
  currency: 'USD',
  price_source: 'demo',
  symbol: 'NVDA',
  price_change_percent: '-10',
  price_before: '150.00',
  price_after: '135.00',
  holding_value_before: '1500.00',
  holding_value_after: '1350.00',
  total_value_before: '16100.00',
  total_value_after: '15950.00',
  change_value: '-150.00',
  change_percent: '-0.93',
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

/** Holdings first (fetched on mount), then whatever the scenario call should return. */
function stubFetch(holdings: unknown = DEMO, scenario: unknown = SCENARIO) {
  const mock = vi.fn((url: string) =>
    Promise.resolve(
      String(url).includes('/scenario')
        ? jsonResponse(scenario)
        : jsonResponse(holdings),
    ),
  )
  vi.stubGlobal('fetch', mock)
  return mock
}

/** Every request fails the same way, for the load-error cases. */
function stubFailingFetch(body: unknown, status: number) {
  const mock = vi.fn().mockResolvedValue(jsonResponse(body, status))
  vi.stubGlobal('fetch', mock)
  return mock
}

/** The scenario request the page actually sent. */
function sentScenario(mock: ReturnType<typeof vi.fn>) {
  const call = mock.mock.calls.find(([url]) => String(url).includes('/scenario'))
  if (!call) throw new Error('no scenario request was sent')
  return {
    url: String(call[0]),
    method: (call[1] as RequestInit | undefined)?.method,
    body: JSON.parse(String((call[1] as RequestInit | undefined)?.body)),
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('WhatIfPage', () => {
  it('shows a loading state before the holdings arrive', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))

    render(<WhatIfPage />)

    expect(screen.getByText('Loading portfolio data')).toBeTruthy()
  })

  it('shows the heading, description, and demo-prices badge', async () => {
    stubFetch()

    render(<WhatIfPage />)
    // The header renders during loading too, but waiting for the request to settle
    // keeps its state update inside the test rather than after it.
    await screen.findByLabelText('Stock')

    expect(screen.getByRole('heading', { level: 1, name: 'What if?' })).toBeTruthy()
    expect(
      screen.getByText(
        'Explore how a hypothetical stock price change could affect your portfolio.',
      ),
    ).toBeTruthy()
    expect(screen.getByText('Demo prices')).toBeTruthy()
  })

  it('populates the stock list from the portfolio holdings, without cash', async () => {
    stubFetch()

    render(<WhatIfPage />)

    const stock = await screen.findByLabelText('Stock')
    const options = within(stock)
      .getAllByRole('option')
      .map((option) => option.textContent)

    expect(options).toEqual(['AAPL', 'MSFT', 'NVDA'])
    // Cash is part of the portfolio value but is not a stock that can be re-priced.
    expect(options).not.toContain('CASH')
  })

  it('bounds the price change input to -100% and +100%', async () => {
    stubFetch()

    render(<WhatIfPage />)

    const input = await screen.findByLabelText('Price change')
    expect(input.getAttribute('min')).toBe('-100')
    expect(input.getAttribute('max')).toBe('100')
    expect(screen.getByText('Use a negative number for a price drop and a positive number for an increase.')).toBeTruthy()
  })

  it('states what the scenario holds constant', async () => {
    stubFetch()

    render(<WhatIfPage />)

    expect(
      await screen.findByText('Other asset prices, cash, and quantities remain unchanged.'),
    ).toBeTruthy()
  })

  it('asks for a scenario before showing any impact', async () => {
    stubFetch()

    render(<WhatIfPage />)

    expect(
      await screen.findByText('Choose a stock and enter a price change to explore its impact.'),
    ).toBeTruthy()
    // Nothing is labelled hypothetical until something has been calculated.
    expect(screen.queryByText('Hypothetical')).toBeNull()
  })

  it('reports a missing portfolio instead of an empty form', async () => {
    stubFailingFetch({ detail: "Portfolio 'AlphaDesk Demo' has not been seeded." }, 404)

    render(<WhatIfPage />)

    expect(await screen.findByText('No portfolio available.')).toBeTruthy()
    expect(screen.getByText(/has not been seeded/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })

  it('surfaces a server error', async () => {
    stubFailingFetch({ detail: 'No demo price available for held symbol(s): XYZ.' }, 503)

    render(<WhatIfPage />)

    expect(await screen.findByText(/No demo price available/)).toBeTruthy()
  })

  it('says there is nothing to model when the portfolio holds no stocks', async () => {
    stubFetch({
      ...DEMO,
      holdings: [],
      holdings_value: '0.00',
      total_value: '10000.00',
      cash_allocation_percent: '100.00',
    })

    render(<WhatIfPage />)

    expect(await screen.findByText('No stock holdings to model.')).toBeTruthy()
    expect(screen.queryByLabelText('Stock')).toBeNull()
  })
})

describe('WhatIfPage calculation', () => {
  /** Pick NVDA and -10%, which is the example the demo data is documented against. */
  async function chooseNvdaDown10() {
    await screen.findByLabelText('Stock')
    fireEvent.change(screen.getByLabelText('Stock'), { target: { value: 'NVDA' } })
    fireEvent.change(screen.getByLabelText('Price change'), { target: { value: '-10' } })
  }

  it('sends the chosen symbol and percentage as a string', async () => {
    const mock = stubFetch()
    render(<WhatIfPage />)
    await chooseNvdaDown10()

    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))
    await screen.findByText('Hypothetical')

    expect(sentScenario(mock)).toEqual({
      url: '/api/portfolio/scenario',
      method: 'POST',
      // A string, not -10 as a number: the backend parses it straight to Decimal.
      body: { symbol: 'NVDA', price_change_percent: '-10' },
    })
  })

  it('shows the price, holding value, and portfolio value before and after', async () => {
    stubFetch()
    render(<WhatIfPage />)
    await chooseNvdaDown10()

    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))

    const row = async (label: string) => {
      const term = await screen.findByText(label)
      return term.closest('div')?.textContent ?? ''
    }

    expect(await row('NVDA price')).toContain('$150.00')
    expect(await row('NVDA price')).toContain('$135.00')
    expect(await row('Holding value')).toContain('$1,500.00')
    expect(await row('Holding value')).toContain('$1,350.00')
    expect(await row('Portfolio value')).toContain('$16,100.00')
    expect(await row('Portfolio value')).toContain('$15,950.00')
  })

  it('states the change with a sign, a percentage, and a word', async () => {
    stubFetch()
    render(<WhatIfPage />)
    await chooseNvdaDown10()

    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))

    // "decrease" is what carries the direction when the colour does not: in greyscale,
    // or through a screen reader, red and green say nothing at all.
    const change = await screen.findByText(/—\s*decrease/)
    expect(change.textContent).toContain('-$150.00')
    expect(change.textContent).toContain('-0.93%')
  })

  it('says increase for a rise', async () => {
    stubFetch(DEMO, {
      ...SCENARIO,
      price_change_percent: '10',
      price_after: '165.00',
      holding_value_after: '1650.00',
      total_value_after: '16250.00',
      change_value: '150.00',
      change_percent: '0.93',
    })
    render(<WhatIfPage />)
    await chooseNvdaDown10()

    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))

    const change = await screen.findByText(/—\s*increase/)
    expect(change.textContent).toContain('$150.00')
    expect(change.textContent).toContain('0.93%')
  })

  it('labels the figures hypothetical, and only while they are shown', async () => {
    stubFetch()
    render(<WhatIfPage />)
    await chooseNvdaDown10()

    expect(screen.queryByText('Hypothetical')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))

    expect(await screen.findByText('Hypothetical')).toBeTruthy()
  })

  it('refuses a percentage outside -100 to +100 without calling the API', async () => {
    const mock = stubFetch()
    render(<WhatIfPage />)
    await screen.findByLabelText('Price change')

    fireEvent.change(screen.getByLabelText('Price change'), { target: { value: '150' } })
    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))

    expect(await screen.findByText('Enter a percentage between -100 and 100.')).toBeTruthy()
    expect(mock.mock.calls.some(([url]) => String(url).includes('/scenario'))).toBe(false)
  })

  it('refuses an empty percentage without calling the API', async () => {
    const mock = stubFetch()
    render(<WhatIfPage />)
    await screen.findByLabelText('Price change')

    fireEvent.change(screen.getByLabelText('Price change'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))

    expect(await screen.findByText('Enter a percentage to explore.')).toBeTruthy()
    expect(mock.mock.calls.some(([url]) => String(url).includes('/scenario'))).toBe(false)
  })

  it('reports a failed calculation instead of leaving stale figures up', async () => {
    const mock = vi.fn((url: string) =>
      Promise.resolve(
        String(url).includes('/scenario')
          ? jsonResponse({ detail: 'TSLA is not held by \'AlphaDesk Demo\'.' }, 404)
          : jsonResponse(DEMO),
      ),
    )
    vi.stubGlobal('fetch', mock)

    render(<WhatIfPage />)
    await chooseNvdaDown10()

    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))

    expect(await screen.findByText(/is not held by/)).toBeTruthy()
    expect(screen.queryByText('Hypothetical')).toBeNull()
  })

  it('clears figures from a previous scenario when the stock changes', async () => {
    stubFetch()
    render(<WhatIfPage />)
    await chooseNvdaDown10()

    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))
    await screen.findByText('Hypothetical')

    fireEvent.change(screen.getByLabelText('Stock'), { target: { value: 'AAPL' } })

    // NVDA's numbers must not stay on screen under a form that now says AAPL.
    expect(screen.queryByText('Hypothetical')).toBeNull()
    expect(
      screen.getByText('Choose a stock and enter a price change to explore its impact.'),
    ).toBeTruthy()
  })

  it('clears figures from a previous scenario when the percentage changes', async () => {
    stubFetch()
    render(<WhatIfPage />)
    await chooseNvdaDown10()

    fireEvent.click(screen.getByRole('button', { name: 'Calculate impact' }))
    await screen.findByText('Hypothetical')

    fireEvent.change(screen.getByLabelText('Price change'), { target: { value: '-20' } })

    expect(screen.queryByText('Hypothetical')).toBeNull()
  })
})
