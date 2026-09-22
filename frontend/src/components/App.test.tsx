/** Moving between the two destinations, and the mobile drawer closing behind you. */

import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import type { Valuation } from '../api'

/** The shape `GET /portfolio/valuation` returns for the demo portfolio. */
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

/** Every request succeeds. Unlike the page tests, moving between two pages makes an
 *  unpredictable number of calls, so this one always resolves rather than counting. */
function stubFetch(value: unknown = DEMO, status = 200) {
  const mock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => value,
  } as Response)
  vi.stubGlobal('fetch', mock)
  return mock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('opens on Portfolio', async () => {
    stubFetch()

    render(<App />)

    expect(screen.getByRole('heading', { level: 1, name: 'Portfolio' })).toBeTruthy()
    expect(await screen.findAllByText('$16,100.00')).not.toHaveLength(0)
  })

  it('switches to What if? and moves the highlight with it', async () => {
    stubFetch()

    render(<App />)
    await screen.findAllByText('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: 'What if?' }))
    // Let the newly mounted page finish its own request, so its state update lands
    // inside the test rather than after it.
    await screen.findByLabelText('Stock')

    expect(screen.getByRole('heading', { level: 1, name: 'What if?' })).toBeTruthy()
    expect(screen.queryByRole('heading', { level: 1, name: 'Portfolio' })).toBeNull()
    expect(screen.getByRole('button', { name: 'What if?' }).getAttribute('aria-current')).toBe(
      'page',
    )
    expect(screen.getByRole('button', { name: 'Portfolio' }).getAttribute('aria-current')).toBeNull()
  })

  it('leaves Portfolio independently reachable from What if?', async () => {
    stubFetch()

    render(<App />)
    await screen.findAllByText('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: 'What if?' }))
    expect(screen.getByRole('heading', { level: 1, name: 'What if?' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Portfolio' }))

    expect(screen.getByRole('heading', { level: 1, name: 'Portfolio' })).toBeTruthy()
    expect(screen.queryByRole('heading', { level: 1, name: 'What if?' })).toBeNull()
    expect(await screen.findAllByText('$16,100.00')).not.toHaveLength(0)
  })

  it('closes the mobile drawer after a destination is chosen in it', async () => {
    stubFetch()

    render(<App />)
    await screen.findAllByText('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: 'Open navigation menu' }))
    const drawer = screen.getByRole('dialog')

    fireEvent.click(within(drawer).getByRole('button', { name: 'What if?' }))
    await screen.findByLabelText('Stock')

    // Both at once: the destination changed and the drawer got out of the way.
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('heading', { level: 1, name: 'What if?' })).toBeTruthy()
  })
})
