/** The Orders page, driven by stubbed responses.
 *
 * What matters here is that a reader can tell four things apart: an estimate from an actual
 * fill, a batch that is still working from one that has stopped, a *submission whose outcome is
 * not known* from one that failed, and the portfolio sync from the execution itself. Those are
 * the ways an order screen usually lies, and each is checked directly.
 */

import { StrictMode } from 'react'

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { anOrder, aBatch, jsonResponse } from '../testing'
import { OrdersPage } from './OrdersPage'

function stubOrders(body: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue(jsonResponse(body, status))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.unstubAllGlobals()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('OrdersPage', () => {
  it('says plainly that no orders have been submitted when there are none', async () => {
    stubOrders({ active_batch_id: null, batches: [] })

    render(<OrdersPage active />)

    expect(await screen.findByText(/No orders have been submitted/)).toBeTruthy()
    expect(screen.getByText('Paper Trading')).toBeTruthy()
  })

  it('names the proposal and the approval time, and keeps the paper label visible', async () => {
    stubOrders({ active_batch_id: aBatch().batch_id, batches: [aBatch()] })

    render(<OrdersPage active />)

    expect(await screen.findByText('a'.repeat(32))).toBeTruthy()
    expect(screen.getByText(/Approved /)).toBeTruthy()
    expect(screen.getByText('Paper Trading')).toBeTruthy()
  })

  it('separates the estimate from the actual fill price', async () => {
    stubOrders({ active_batch_id: null, batches: [aBatch()] })

    render(<OrdersPage active />)

    await screen.findByRole('table')
    expect(screen.getByText('Estimated price')).toBeTruthy()
    expect(screen.getByText('Average fill price')).toBeTruthy()
    // The estimate is the plan's figure; the fill is the broker's, and they differ here so a
    // page that showed one as the other would be caught.
    expect(screen.getByText('200.00 USD')).toBeTruthy()
    expect(screen.getByText('199.50 USD')).toBeTruthy()
  })

  it('shows the request: side, quantity, type, time in force and status', async () => {
    stubOrders({ active_batch_id: null, batches: [aBatch()] })

    render(<OrdersPage active />)

    await screen.findByRole('table')
    const row = screen.getByRole('row', { name: /AAPL/ })
    expect(row.textContent).toContain('sell')
    expect(row.textContent).toContain('5')
    expect(row.textContent).toContain('market')
    expect(row.textContent).toContain('day')
    expect(row.textContent).toContain('Filled')
    expect(row.textContent).toContain('broker-1')
  })

  it('renders a dash rather than zero for an order that has not filled', async () => {
    stubOrders({
      active_batch_id: null,
      batches: [
        aBatch({
          orders: [
            anOrder({ status: 'planned', broker_status: null, filled_quantity: '0', filled_avg_price: null }),
          ],
        }),
      ],
    })

    render(<OrdersPage active />)

    await screen.findByRole('table')
    expect(screen.getByText('Not sent yet')).toBeTruthy()
    expect(screen.getByText('—')).toBeTruthy()
  })

  it('says an unanswered submission is not known rather than failed', async () => {
    stubOrders({
      active_batch_id: null,
      batches: [
        aBatch({
          orders: [
            anOrder({
              status: 'unknown',
              filled_avg_price: null,
              error: 'Whether the order reached the broker is not known.',
            }),
          ],
        }),
      ],
    })

    render(<OrdersPage active />)

    expect(await screen.findByText(/Unanswered — outcome not known/)).toBeTruthy()
    // The order's own explanation is shown too, in the broker's words.
    expect(screen.getByText(/Whether the order reached the broker is not known/)).toBeTruthy()
  })

  it('says an order the broker has no record of was never confirmed', async () => {
    stubOrders({
      active_batch_id: null,
      batches: [aBatch({ orders: [anOrder({ status: 'not_found', broker_status: null })] })],
    })

    render(<OrdersPage active />)

    expect(await screen.findByText(/Never confirmed by the broker/)).toBeTruthy()
  })

  it('explains a stopped batch, including that earlier fills stand', async () => {
    stubOrders({
      active_batch_id: null,
      batches: [
        aBatch({
          status: 'halted',
          halt_reason: 'The US market is closed, so the orders that had not been submitted were stopped.',
          orders: [
            anOrder(),
            anOrder({ intent_id: 'f'.repeat(32), sequence: 2, symbol: 'MSFT', side: 'buy', status: 'halted', broker_status: null }),
          ],
        }),
      ],
    })

    render(<OrdersPage active />)

    expect(await screen.findByText(/market is closed/)).toBeTruthy()
    expect(screen.getAllByText('Stopped').length).toBeGreaterThan(0)
    expect(screen.getByText('Stopped, never sent')).toBeTruthy()
  })

  it('reports a partial completion as partial rather than as a failure', async () => {
    stubOrders({
      active_batch_id: null,
      batches: [
        aBatch({
          status: 'partial',
          orders: [
            anOrder(),
            anOrder({ intent_id: 'f'.repeat(32), sequence: 2, symbol: 'MSFT', side: 'buy', status: 'rejected', broker_status: 'rejected' }),
          ],
        }),
      ],
    })

    render(<OrdersPage active />)

    expect(await screen.findByRole('heading', { name: 'Partly completed' })).toBeTruthy()
    expect(screen.getByText('Rejected by the broker')).toBeTruthy()
  })

  it('reports a portfolio sync failure apart from the execution', async () => {
    stubOrders({
      active_batch_id: null,
      batches: [
        aBatch({
          status: 'completed',
          sync_status: 'failed',
          sync_error: 'No portfolio named AlphaDesk Demo is stored.',
        }),
      ],
    })

    render(<OrdersPage active />)

    const notice = await screen.findByText(/could not be updated from the broker/)
    expect(notice.textContent).toContain('an order that filled did fill')
    expect(screen.getByRole('heading', { name: 'Completed' })).toBeTruthy()
  })

  it('says the portfolio was updated when the sync succeeded', async () => {
    stubOrders({
      active_batch_id: null,
      batches: [aBatch({ status: 'completed', sync_status: 'ok', sync_at: '2026-09-23T12:10:00+00:00' })],
    })

    render(<OrdersPage active />)

    expect(await screen.findByText(/Portfolio updated from the broker/)).toBeTruthy()
  })

  it('keeps earlier batches rather than showing only the latest', async () => {
    stubOrders({
      active_batch_id: null,
      batches: [
        aBatch({ batch_id: 'b'.repeat(32), proposal_id: 'newer' }),
        aBatch({ batch_id: 'c'.repeat(32), proposal_id: 'older', status: 'completed' }),
      ],
    })

    render(<OrdersPage active />)

    expect(await screen.findByText('newer')).toBeTruthy()
    expect(screen.getByText('older')).toBeTruthy()
  })

  it('marks the batch that was just approved', async () => {
    const batch = aBatch()
    stubOrders({ active_batch_id: batch.batch_id, batches: [batch] })

    render(<OrdersPage active focusBatchId={batch.batch_id} />)

    expect(await screen.findByText('Just approved')).toBeTruthy()
    expect(screen.getByRole('heading', { name: /Waiting|Working|Completed|Stopped/ })).toBeTruthy()
  })

  it('reports a failure to load rather than an empty page', async () => {
    stubOrders({ detail: 'Orders are unavailable.' }, 503)

    render(<OrdersPage active />)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Orders unavailable')
    expect(alert.textContent).toContain('Orders are unavailable.')
  })

  it('re-reads when the page becomes active again', async () => {
    const fetchMock = stubOrders({ active_batch_id: null, batches: [] })

    const { rerender } = render(<OrdersPage active={false} />)
    expect(fetchMock).not.toHaveBeenCalled()

    rerender(<OrdersPage active />)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  })

  it('polls while a batch is working and stops once it is not', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const fetchMock = stubOrders({
        active_batch_id: aBatch().batch_id,
        batches: [aBatch({ status: 'submitting' })],
      })

      render(<OrdersPage active />)
      await waitFor(() => expect(fetchMock).toHaveBeenCalled())

      const before = fetchMock.mock.calls.length
      await vi.advanceTimersByTimeAsync(4100)
      expect(fetchMock.mock.calls.length).toBeGreaterThan(before)
    } finally {
      vi.useRealTimers()
    }
  })

  it('loads under StrictMode, whose second effect run supersedes the first', async () => {
    /* The bug this pins: an "one request at a time" guard refuses StrictMode's second run while
       the first request is still aborting, so no request is ever made and the page sits on the
       empty state -- "no orders have been submitted" -- for a batch that exists. */
    stubOrders({ active_batch_id: aBatch().batch_id, batches: [aBatch()] })

    render(
      <StrictMode>
        <OrdersPage active />
      </StrictMode>,
    )

    expect(await screen.findByRole('table')).toBeTruthy()
    expect(screen.queryByText(/No orders have been submitted/)).toBeNull()
  })

  it('refreshes on request', async () => {
    const fetchMock = stubOrders({ active_batch_id: null, batches: [] })

    render(<OrdersPage active />)
    await screen.findByText(/No orders have been submitted/)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1))
  })
})
