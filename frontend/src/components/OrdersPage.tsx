import { Fragment, useCallback, useEffect, useRef, useState } from 'react'

import {
  ApiError,
  fetchOrders,
  type ExecutionBatch,
  type OrderIntent,
  type OrdersView,
} from '../api'
import { batchColour, batchLabel, intentColour, intentLabel, POLL_INTERVAL_MS } from '../orders'
import { Button } from './Button'
import { Card } from './Card'
import { ErrorNotice, LoadingCards } from './States'

/** How long the "just approved" ring and label stay, before both leave.
 *
 * The highlight answers one question -- "which batch did the approval I just made open?" --
 * and a batch approved ten minutes ago is not the answer to anything. So it is a moment, not
 * a state: the stylesheet fades it over this same clock (`approval-ring-out` and
 * `approval-label-out` in index.css) and this timer takes it out of the DOM at the end of it.
 * Removing it matters as much as fading it: a label at `opacity: 0` is still a label a screen
 * reader reads out.
 *
 * The two halves are handed the same number rather than each carrying its own copy, because
 * a ring that finishes fading after the element has gone -- or an element that vanishes
 * mid-fade -- is the bug that pairing invites.
 */
export const APPROVAL_HIGHLIGHT_MS = 3000

/** The order table's columns, counted once: the ten the order carries plus the one the
 *  chevron sits in. The detail row spans all of them, and a `colSpan` that disagrees with
 *  the header is a table that renders as a ragged grid rather than as an error. */
const ORDER_COLUMNS = 11

/** The id the detail card carries, so the row's button can point at what it opens. */
function panelId(intentId: string): string {
  return `order-detail-${intentId}`
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
  // Which order's detail card is open. One at a time: the card is a restatement of the row
  // that opened it, so a second open one is a second thing to scroll past rather than a
  // second thing to compare with. Clicking the open order again closes it.
  const [openOrderId, setOpenOrderId] = useState<string | null>(null)

  // The ring the approval left behind, for as long as it is still news. `focused` says which
  // batch that was; this says whether the moment is still running.
  const [highlighted, setHighlighted] = useState(focused)

  useEffect(() => {
    if (!focused) {
      setHighlighted(false)
      return
    }
    setHighlighted(true)
    const timer = window.setTimeout(() => setHighlighted(false), APPROVAL_HIGHLIGHT_MS)
    return () => window.clearTimeout(timer)
  }, [focused])

  const toggle = (intentId: string) =>
    setOpenOrderId((current) => (current === intentId ? null : intentId))

  return (
    <Card>
      <div
        className={highlighted ? 'approval-ring rounded-md' : undefined}
        style={highlighted ? { animationDuration: `${APPROVAL_HIGHLIGHT_MS}ms` } : undefined}
        data-testid={`batch-${batch.batch_id}`}
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            {/* The status is the heading, in its own colour. It is the first thing a reader
                wants and the only thing this card leads with. */}
            <h2 className={`text-heading font-medium ${batchColour(batch)}`}>
              {batchLabel(batch)}
              {highlighted && (
                <span
                  className="approval-label ml-2 text-label text-accent-ink"
                  style={{ animationDuration: `${APPROVAL_HIGHLIGHT_MS}ms` }}
                >
                  Just approved
                </span>
              )}
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
                <th className="py-2">
                  <span className="sr-only">Details</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {batch.orders.map((order) => {
                const open = order.intent_id === openOrderId
                return (
                  <Fragment key={order.intent_id}>
                    <tr
                      /* The row is the target, because a reader who wants one order's detail
                         is pointing at the order, not at a control inside it. A click on the
                         row and Enter on the button below do the same thing; the button is
                         what a keyboard and a screen reader reach, and it is where the
                         expanded state is announced from. */
                      onClick={() => toggle(order.intent_id)}
                      className="cursor-pointer border-b border-line/60 align-top transition-colors hover:bg-selected/40"
                    >
                      <td className="py-2 pr-3 font-medium text-ink">
                        <button
                          type="button"
                          aria-expanded={open}
                          aria-controls={panelId(order.intent_id)}
                          onClick={(event) => {
                            event.stopPropagation()
                            toggle(order.intent_id)
                          }}
                          className="font-medium text-ink"
                        >
                          {order.symbol}
                        </button>
                      </td>
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
                      <td className="py-2 pr-3 text-dim">
                        {money(order.estimated_price, batch.currency)}
                      </td>
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
                      {/* Shortened here and given in full in the card below, the way the
                          batch id is shortened at the top of this card. A 36-character id is
                          the widest thing in the row by a factor of two, and left whole it
                          sets the table's minimum width on its own -- which is what turns
                          one row opening into a scrollbar under the whole batch. */}
                      <td className="py-2 pl-3 font-mono text-label text-faint">
                        {order.broker_order_id === null ? (
                          '—'
                        ) : (
                          <span title={order.broker_order_id}>
                            {shortenedId(order.broker_order_id)}
                          </span>
                        )}
                      </td>
                      {/* Points at the card rather than naming it: the row already says
                          which order this is, and a chevron that turns is the whole
                          convention for "there is more under this". */}
                      <td className="py-2 pl-2 text-right text-faint">
                        <ChevronIcon open={open} />
                      </td>
                    </tr>

                    {open && (
                      <tr className="align-top">
                        <td colSpan={ORDER_COLUMNS} className="pb-3 pt-1">
                          <OrderDetail order={order} currency={batch.currency} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
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

/** One order, opened out of its row.
 *
 * The table has to fit ten columns into a strip, so it sets the columns a reader compares
 * across. This says the same fields at a size that can be read one at a time, which is what
 * somebody wants when they are looking at a single order rather than at the batch.
 *
 * Nothing here is new information and nothing is recomputed: every value is the row's own,
 * through the same `shares` and `money` helpers, so the card cannot come to disagree with
 * the line it was opened from.
 */
function OrderDetail({ order, currency }: { order: OrderIntent; currency: string }) {
  return (
    <div
      id={panelId(order.intent_id)}
      className="rounded-card border border-line bg-page p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-heading font-medium text-ink">
          {/* The space is written out: JSX drops the whitespace around a line break, so
              `{order.symbol}` followed by the side on the next line would read as one word
              -- "AAPLsell" -- to anyone selecting the text or hearing it read out. */}
          {order.symbol}{' '}
          <span
            className={`ml-2 ${order.side === 'buy' ? 'text-positive' : 'text-danger'}`}
          >
            {order.side}
          </span>
        </h3>
        {/* The order's own id: the batch id is on the card above, and this is the one the
            broker and the execution plan are keyed by. */}
        <p className="font-mono text-label text-faint" title={order.intent_id}>
          {order.intent_id.slice(0, 12)}
        </p>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-label min-[700px]:grid-cols-4">
        <Figure label="Quantity" value={shares(order.quantity)} />
        <Figure label="Filled" value={shares(order.filled_quantity)} />
        <Figure label="Order type" value={order.order_type} valueClassName="text-dim" />
        <Figure label="Time in force" value={order.time_in_force} valueClassName="text-dim" />
        <Figure
          label="Estimated price"
          value={money(order.estimated_price, currency)}
          valueClassName="text-dim"
        />
        <Figure
          label="Average fill price"
          value={order.filled_avg_price === null ? '—' : money(order.filled_avg_price, currency)}
        />
        <Figure
          label="Broker status"
          value={statusWithBroker(order)}
          valueClassName={intentColour(order)}
        />
        {/* `break-words`, not `break-all`: a hyphenated id wraps at its hyphens, which reads
            as the groups it is made of, and only an id with no hyphens at all is broken
            mid-word -- rather than widening the table it sits in. */}
        <Figure
          label="Broker order id"
          value={order.broker_order_id ?? '—'}
          valueClassName="font-mono text-faint break-words"
        />
      </dl>
    </div>
  )
}

/** The disclosure chevron on an order row.
 *
 * Drawn rather than typed, and that is the whole reason it is a component. "▸" is a font
 * glyph, so its box is whatever the font decides it is -- about 19px here, in a column that
 * is 26px wide including its padding. Left as a glyph it sticks out past the table's right
 * edge and drags a horizontal scrollbar under a table that had been fitting, which is a
 * large consequence for a small triangle. An SVG is exactly the size it says it is.
 */
function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 12 12"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={`inline-block shrink-0 transition-transform ${open ? 'rotate-90' : ''}`}
    >
      <path d="M4.5 2.5L8 6l-3.5 3.5" />
    </svg>
  )
}

/** An id, cut to twelve characters for the row -- with the ellipsis only when something was
 *  actually cut, so a short id is never made to look like a longer one. */
function shortenedId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value
}

/** The status, and the broker's own word for it in brackets when there is one.
 *
 * The row shows the two side by side because they are two columns; here they are one line,
 * which is how somebody reading a single order says it out loud. */
function statusWithBroker(order: OrderIntent): string {
  const label = intentLabel(order)
  return order.broker_status === null ? label : `${label} (${order.broker_status})`
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

/** `valueClassName` exists for the two figures that are not plain ink: a side that is a rise
 *  or a fall, and a status that is one of the same. The default is what every other figure
 *  already used.
 *
 *  `min-w-0` is the grid's own default being overruled on purpose. A grid item is
 *  `min-width: auto`, which means it refuses to shrink below the width of its longest word --
 *  and the order detail card lives in a row that spans the whole table, so a figure that
 *  would not shrink would widen the table and put a horizontal scrollbar under it. */
function Figure({
  label,
  value,
  valueClassName = 'text-ink',
}: {
  label: string
  value: string
  valueClassName?: string
}) {
  return (
    <div className="min-w-0">
      <dt className="text-faint">{label}</dt>
      <dd className={valueClassName}>{value}</dd>
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
