/** Moving between destinations, and the mobile drawer closing behind you. */

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import type { Valuation } from '../api'
import { AURORA_FADE_MS, AURORA_MINIMUM_MS } from './AuroraSplash'
import { aStrategy } from '../testing'

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

/** Every request succeeds. Unlike the page tests, moving between destinations makes an
 *  unpredictable number of calls, so this one always resolves rather than counting -- and it
 *  answers by URL, because the Backtest page reads a strategy catalogue whose shape the
 *  portfolio fixture is not. Anything it does not know about gets `value`. */
function stubFetch(value: unknown = DEMO, status = 200) {
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()
    const body = url.includes('/backtest/strategies')
      ? { strategies: [aStrategy()] }
      : url.includes('/backtest/runs/latest')
        ? { run: null }
        : value
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    } as Response
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

/** The same stub, with the portfolio reads held open until the test lets them go.
 *
 * Both `/portfolio/sync` and `/portfolio/valuation` are held: they are the two halves of the
 * one read the startup screen is waiting for, and holding only one would let the other settle
 * the screen while the test still thought the page was loading.
 */
function stubHeldPortfolio() {
  // Released once and for all, not per request: the read is a sync *and* a valuation, and
  // letting the first through only would leave the second holding the page for ever.
  let held = true
  const waiting: Array<() => void> = []
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (held && url.includes('/portfolio/')) {
      await new Promise<void>((resolve) => waiting.push(resolve))
    }
    const body = url.includes('/backtest/strategies')
      ? { strategies: [aStrategy()] }
      : url.includes('/backtest/runs/latest')
        ? { run: null }
        : DEMO
    return { ok: true, status: 200, json: async () => body } as Response
  })
  vi.stubGlobal('fetch', mock)
  return {
    release: () => {
      held = false
      waiting.splice(0).forEach((resolve) => resolve())
    },
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

/** Runs the startup screen's clock out: past its minimum, and then past its fade.
 *
 * Two advances and not one, because the fade's timer does not exist until the minimum has
 * passed *and* React has run the effect that starts it -- and one tick cannot reach a timer
 * that is only created part-way through it. The `act` around each is what lets that effect run
 * between them. Call it only with the clock faked.
 */
async function runOutTheStartupScreen() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(AURORA_MINIMUM_MS + 50)
  })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(AURORA_FADE_MS + 50)
  })
}

describe('App', () => {
  it('opens on Portfolio', async () => {
    stubFetch()

    render(<App />)

    expect(screen.getByRole('heading', { level: 1, name: 'Portfolio' })).toBeTruthy()
    expect(await screen.findAllByText('$16,100.00')).not.toHaveLength(0)
  })

  it('switches to Backtest and moves the highlight with it', async () => {
    stubFetch()

    render(<App />)
    await screen.findAllByText('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: 'Backtest' }))
    // Let the newly mounted page finish its own requests, so its state updates land
    // inside the test rather than after it.
    await screen.findByRole('button', { name: /Moving Average Crossover/ })

    expect(screen.getByRole('heading', { level: 1, name: 'Backtest' })).toBeTruthy()
    expect(screen.queryByRole('heading', { level: 1, name: 'Portfolio' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Backtest' }).getAttribute('aria-current')).toBe(
      'page',
    )
    expect(screen.getByRole('button', { name: 'Portfolio' }).getAttribute('aria-current')).toBeNull()
  })

  it('leaves Portfolio independently reachable from Backtest', async () => {
    stubFetch()

    render(<App />)
    await screen.findAllByText('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: 'Backtest' }))
    expect(screen.getByRole('heading', { level: 1, name: 'Backtest' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Portfolio' }))

    expect(screen.getByRole('heading', { level: 1, name: 'Portfolio' })).toBeTruthy()
    expect(screen.queryByRole('heading', { level: 1, name: 'Backtest' })).toBeNull()
    expect(await screen.findAllByText('$16,100.00')).not.toHaveLength(0)
  })

  // The startup screen. It waits for two things -- the first page's own read, which is why
  // there is no separate startup call, and its own minimum -- so these drive the read and the
  // clock rather than waiting on either.
  //
  // The clock is faked in all three: the minimum is seconds of real time, and three tests
  // sleeping through it is most of a test run spent watching nothing happen. `shouldAdvanceTime`
  // keeps the clock moving with real time as well, which is what `findBy*` needs to poll.
  it('covers the workspace while the first page is reading, then uncovers it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const { release } = stubHeldPortfolio()

      render(<App />)

      expect(screen.getByText('Loading your intelligent trading workspace')).toBeTruthy()
      // A status, so a screen reader is told what is happening. `getAllBy` because the page
      // underneath has a status of its own -- it is loading too, which is the whole point.
      expect(screen.getAllByRole('status').map((el) => el.textContent)).toContain(
        'Loading your intelligent trading workspace',
      )
      expect(screen.queryByText('$16,100.00')).toBeNull()

      await act(async () => {
        release()
      })

      // The read is back, and the screen is still up: what it is waiting for now is its own
      // minimum. Without that, a read this fast would take the screen away in a flash.
      expect(screen.getByText('Loading your intelligent trading workspace')).toBeTruthy()

      await runOutTheStartupScreen()

      expect(screen.queryByText('Loading your intelligent trading workspace')).toBeNull()
      expect(screen.getAllByText('$16,100.00')).not.toHaveLength(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not cover the workspace again when the page refreshes behind it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      stubFetch()

      render(<App />)
      await runOutTheStartupScreen()
      expect(screen.queryByText('Loading your intelligent trading workspace')).toBeNull()

      // A refresh is the same read this page makes on every tick, and the reader is already
      // looking at the workspace. Only the first one is the startup.
      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
      await screen.findAllByText('$16,100.00')

      expect(screen.queryByText('Loading your intelligent trading workspace')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('gives up the startup screen when the first read fails, rather than animating for ever', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      stubFetch({ detail: 'The API is unavailable.' }, 503)

      render(<App />)

      await runOutTheStartupScreen()

      // The failure is what the reader gets. A screen that kept spinning would hide the one
      // thing they could act on.
      expect(screen.queryByText('Loading your intelligent trading workspace')).toBeNull()
      expect(screen.getByText('Could not load the portfolio.')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('closes the mobile drawer after a destination is chosen in it', async () => {
    stubFetch()

    render(<App />)
    await screen.findAllByText('$16,100.00')

    fireEvent.click(screen.getByRole('button', { name: 'Open navigation menu' }))
    const drawer = screen.getByRole('dialog')

    fireEvent.click(within(drawer).getByRole('button', { name: 'Backtest' }))
    await screen.findByRole('button', { name: /Moving Average Crossover/ })

    // Both at once: the destination changed and the drawer got out of the way.
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('heading', { level: 1, name: 'Backtest' })).toBeTruthy()
  })
})
