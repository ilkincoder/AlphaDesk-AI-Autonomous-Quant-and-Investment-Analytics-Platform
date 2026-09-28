import { useCallback, useEffect, useRef, useState } from 'react'

import {
  ApiError,
  fetchOrders,
  type ExecutionBatch,
  type OrderIntent,
  type OrdersView,
} from '../api'
import { Button } from './Button'
import { Card } from './Card'
import { ErrorNotice, LoadingCards } from './States'

/** How often the page re-reads while a batch is still working.
 *
 * The execution itself does not depend on this: the server follows the batch on its own thread
 * whether or not this page is open. This only decides how quickly the screen catches up.
 */
const POLL_INTERVAL_MS = 4000

/** What a batch's status says to a reader.
 *
 * Plain language rather than the wire value, and never only a colour. `partial` and `halted` are
 * not failures of the application: they mean the batch stopped, and the reason is in the notes.
 */
function batchLabel(batch: ExecutionBatch): string {
  switch (batch.status) {
    case 'completed':
      return 'Completed'
    case 'partial':
      return 'Partly completed'
    case 'halted':
      return 'Stopped'
    case 'submitting':
      return 'Working'
    default:
      return 'Waiting'
  }
}

function batchColour(batch: ExecutionBatch): string {
  if (batch.status === 'completed') return 'text-positive'
  if (batch.status === 'halted' || batch.status === 'partial') return 'text-danger'
  return 'text-accent-ink'
}

/** What one order's status says, including the two that mean "not known".
 *
 * `unknown` is an unanswered submission and `not_found` is one the broker has no record of.
 * Both are shown as what they are rather than as a failure, because an order that may exist
 * must not be reported as one that did not happen.
 */
function intentLabel(intent: OrderIntent): string {
  switch (intent.status) {
    case 'filled':
      return 'Filled'
    case 'partially_filled':
      return 'Partly filled'
    case 'accepted':
      return 'Accepted, not yet filled'
    case 'submitting':
      return 'Being submitted'
    case 'planned':
      return 'Not sent yet'
    case 'rejected':
      return 'Rejected by the broker'
    case 'canceled':
      return 'Canceled'
    case 'expired':
      return 'Expired unfilled'
    case 'halted':
      return 'Stopped, never sent'
    case 'unknown':
      return 'Unanswered — outcome not known'
    case 'not_found':
      return 'Never confirmed by the broker'
    default:
      return intent.status
  }
}

function intentColour(intent: OrderIntent): string {
  if (intent.status === 'filled') return 'text-positive'
  if (['rejected', 'canceled', 'expired', 'halted'].includes(intent.status)) return 'text-danger'
  if (['unknown', 'not_found'].includes(intent.status)) return 'text-danger'
  return 'text-accent-ink'
}

export function OrdersPage({
  active,
  focusBatchId,
}: {
  /** False while another page is showing. Flipping it back re-reads, which is what makes this
   *  page current again when somebody returns to it. */
  active?: boolean
  /** The batch an approval just opened, scrolled to and marked. */
  focusBatchId?: string | null
}) {
  const [view, setView] = useState<OrdersView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  // Whether a batch is still working, kept as its own boolean rather than read off `view` inside
  // the effect below. A fresh `view` is a new object every time, so depending on it re-runs the
  // effect after every successful load -- which aborts the request it just made and starts
  // another, for ever. A boolean only changes when the answer does.
  const [working, setWorking] = useState(false)
  // The request in flight, if any. A new load *supersedes* the one before it rather than being
  // refused by it, which is the same trade `NewsPage`'s listing makes and for the same reason:
  // StrictMode runs an effect, cleans it up and runs it again in development, so a guard that
  // said "one at a time" would refuse the second run while the first was still aborting -- and
  // the page would sit empty for ever, having never made a request at all.
  const inFlight = useRef<AbortController | null>(null)

  const load = useCallback(async () => {
    inFlight.current?.abort()
    const controller = new AbortController()
    inFlight.current = controller
    try {
      const data = await fetchOrders(controller.signal)
      setView(data)
      setWorking(data.batches.some((batch) => ['pending', 'submitting'].includes(batch.status)))
      setError(null)
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return
      setError(cause instanceof ApiError ? cause.message : 'Could not load the orders.')
    } finally {
      // Only the newest request clears the skeleton. An aborted one leaving it cleared would
      // flash the empty state -- "no orders have been submitted" -- over a page still loading.
      if (inFlight.current === controller) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!active) return
    void load()
    // While a batch is working the statuses move on the server, so the page follows. When
    // nothing is running there is nothing to follow and the interval is not started at all.
    const timer = working
      ? window.setInterval(() => void load(), POLL_INTERVAL_MS)
      : undefined
    return () => {
      if (timer !== undefined) window.clearInterval(timer)
      inFlight.current?.abort()
    }
  }, [active, load, working])

  if (loading && view === null) return <LoadingCards />

  if (error !== null && view === null) {
    return <ErrorNotice title="Orders unavailable" message={error} onRetry={() => void load()} />
  }

  const batches = view?.batches ?? []

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-title font-semibold text-ink">Orders</h1>
          <p className="mt-1 text-note text-dim">
            What this application has submitted to the Alpaca paper account, and what the broker
            said about it.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded-full bg-selected px-2.5 py-1 text-label text-accent-ink">
            Paper Trading
          </span>
          <Button onClick={() => void load()}>Refresh</Button>
        </div>
      </header>

      {error !== null && <ErrorNotice title="Could not refresh" message={error} onRetry={() => void load()} />}

      {batches.length === 0 ? (
        <Card>
          <p className="text-body text-dim">
            No orders have been submitted. Approve a rebalance proposal in News &amp; Trading and
            its orders will appear here.
          </p>
        </Card>
      ) : (
        batches.map((batch) => (
          <BatchCard
            key={batch.batch_id}
            batch={batch}
            focused={batch.batch_id === focusBatchId}
          />
        ))
      )}
    </div>
  )
}

function BatchCard({ batch, focused }: { batch: ExecutionBatch; focused: boolean }) {
  return (
    <Card>
      <div
        className={focused ? 'rounded-md ring-1 ring-accent/60' : undefined}
        data-testid={`batch-${batch.batch_id}`}
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            {/* The status is the heading, in its own colour. It is the first thing a reader
                wants and the only thing this card leads with. */}
            <h2 className={`text-heading font-medium ${batchColour(batch)}`}>
              {batchLabel(batch)}
              {focused && <span className="ml-2 text-label text-accent-ink">Just approved</span>}
            </h2>
            <p className="mt-1 text-label text-faint">
              Proposal <span className="font-mono">{batch.proposal_id}</span>
            </p>
            <p className="mt-0.5 text-label text-faint">
              Approved {formatTime(batch.created_at)}
              {batch.completed_at !== null && ` · finished ${formatTime(batch.completed_at)}`}
            </p>
          </div>
          <p className="font-mono text-label text-faint" title={batch.batch_id}>
            {batch.batch_id.slice(0, 12)}
          </p>
        </div>

        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-label min-[700px]:grid-cols-4">
          <Figure label="Orders" value={String(batch.orders.length)} />
          <Figure
            label="Filled"
            value={String(batch.orders.filter((order) => order.status === 'filled').length)}
          />
          <Figure label="Cash at approval" value={money(batch.cash_at_approval, batch.currency)} />
          <Figure label="Held back" value={money(batch.reserved_cash, batch.currency)} />
        </dl>

        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-note">
            <thead>
              <tr className="border-b border-line text-left text-label text-faint">
                <th className="py-2 pr-3 font-medium">Symbol</th>
                <th className="py-2 pr-3 font-medium">Side</th>
                <th className="py-2 pr-3 font-medium">Quantity</th>
                <th className="py-2 pr-3 font-medium">Filled</th>
                <th className="py-2 pr-3 font-medium">Type</th>
                <th className="py-2 pr-3 font-medium">Time in force</th>
                <th className="py-2 pr-3 font-medium">Estimated price</th>
                <th className="py-2 pr-3 font-medium">Average fill price</th>
                <th className="py-2 pr-3 font-medium">Status</th>
                <th className="py-2 font-medium">Broker order id</th>
              </tr>
            </thead>
            <tbody>
              {batch.orders.map((order) => (
                <tr key={order.intent_id} className="border-b border-line/60 align-top">
                  <td className="py-2 pr-3 font-medium text-ink">{order.symbol}</td>
                  <td
                    className={`py-2 pr-3 font-medium ${
                      order.side === 'buy' ? 'text-positive' : 'text-danger'
                    }`}
                  >
                    {order.side}
                  </td>
                  <td className="py-2 pr-3 text-dim">{shares(order.quantity)}</td>
                  <td className="py-2 pr-3 text-dim">{shares(order.filled_quantity)}</td>
                  <td className="py-2 pr-3 text-dim">{order.order_type}</td>
                  <td className="py-2 pr-3 text-dim">{order.time_in_force}</td>
                  {/* Labelled as an estimate in the heading, because that is all it is: the
                      broker's own average is the next column and the only actual price. */}
                  <td className="py-2 pr-3 text-dim">{money(order.estimated_price, batch.currency)}</td>
                  <td className="py-2 pr-3 text-ink">
                    {order.filled_avg_price === null
                      ? '—'
                      : money(order.filled_avg_price, batch.currency)}
                  </td>
                  <td className={`py-2 pr-3 ${intentColour(order)}`}>
                    {intentLabel(order)}
                    {order.broker_status !== null && (
                      <span className="ml-1 text-faint">({order.broker_status})</span>
                    )}
                  </td>
                  <td className="py-2 font-mono text-label text-faint">
                    {order.broker_order_id ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {batch.orders.some((order) => order.error !== null) && (
          <ul className="mt-3 flex flex-col gap-1">
            {batch.orders
              .filter((order) => order.error !== null)
              .map((order) => (
                <li key={`${order.intent_id}-error`} className="text-label text-danger">
                  {order.symbol}: {order.error}
                </li>
              ))}
          </ul>
        )}

        {batch.halt_reason !== null && (
          <p className="mt-3 text-note text-danger">{batch.halt_reason}</p>
        )}

        {batch.notes.length > 0 && (
          <ul className="mt-3 flex flex-col gap-1">
            {batch.notes.map((note) => (
              <li key={note} className="text-label text-dim">
                {note}
              </li>
            ))}
          </ul>
        )}

        <SyncLine batch={batch} />
      </div>
    </Card>
  )
}

/** The portfolio sync, reported apart from the execution.
 *
 * A sync that failed says nothing about whether an order filled, so it is its own line rather
 * than a status on the batch — and it is stated in words, because "the portfolio could not be
 * updated" is the fact a reader needs, not a code.
 */
function SyncLine({ batch }: { batch: ExecutionBatch }) {
  if (batch.sync_status === 'ok') {
    return (
      <p className="mt-3 text-label text-dim">
        Portfolio updated from the broker's own report of the fills
        {batch.sync_at !== null && ` (${formatTime(batch.sync_at)})`}.
      </p>
    )
  }

  if (batch.sync_status === 'failed') {
    return (
      <p className="mt-3 text-label text-danger">
        The portfolio could not be updated from the broker, so the Portfolio page may still show
        the holdings from before this batch. This is separate from the execution above: an order
        that filled did fill. {batch.sync_error}
      </p>
    )
  }

  return (
    <p className="mt-3 text-label text-faint">
      The portfolio has not been updated from this batch yet.
    </p>
  )
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-faint">{label}</dt>
      <dd className="text-ink">{value}</dd>
    </div>
  )
}

/** A quantity, without the six decimal places the column carries. */
function shares(value: string): string {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return value
  return Number.isInteger(parsed) ? String(parsed) : parsed.toFixed(4).replace(/0+$/, '')
}

function money(value: string, currency: string): string {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return value
  return `${parsed.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${currency}`
}

function formatTime(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' })
}
