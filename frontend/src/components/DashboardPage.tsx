/** The Dashboard: what the three modules already hold, in one place.
 *
 * **Nothing here calculates anything.** Every figure is a value an existing endpoint already
 * returned, punctuated for display by `format`. There is no aggregation, no derived metric and
 * no invented number -- where a source has nothing to show, this says so rather than filling the
 * space, because a dashboard that guesses is worse than one that admits a gap.
 *
 * **Four independent sources, fetched at once and applied one at a time.** The portfolio
 * valuation, the execution batches, the latest saved backtest and the latest completed analysis
 * are four separate reads with four separate failure modes; one of them failing must not blank
 * the panels that worked, so each section renders from its own state and carries its own retry.
 *
 * **A read-only page.** Every request it makes is a GET. Loading the Dashboard and pressing
 * Refresh cannot submit an order, generate a proposal, run a backtest, ingest news or call a
 * model -- there is no code here that could, and the tests assert that only GETs are sent.
 *
 * **Loading is not the same as unavailable, and neither is empty.** A source that failed shows
 * `--`, says why and offers a retry; a source that answered and holds nothing shows an empty
 * state with a way to go and create something; a source that answered with zero shows zero.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

import { fetchLatestBacktestRun, fetchOrders, fetchValuation } from '../api'
import type { BacktestRun, ExecutionBatch, OrdersView, Valuation } from '../api'
import { fetchLatestAnalysis } from '../analysisApi'
import type { LatestAnalysis } from '../analysisApi'
import { allocationAssets, nothingToAllocate } from '../allocation'
import { PLACEHOLDER, formatCurrency, formatDateTime, formatSignedPercent } from '../format'
import { AllocationDonut } from './AllocationDonut'
import { Banner } from './States'
import { Button } from './Button'
import { Card } from './Card'
import { EquityChart } from './EquityChart'
import type { PageId } from './Sidebar'
import { ErrorNotice } from './States'

/** A batch status that means the batch is still working.
 *
 * Mirrors `app.orders.ACTIVE_BATCH_STATUSES`. Repeated rather than fetched because it is a display
 * rule, not a figure: the alternative is an endpoint that exists to spell two words the API's own
 * status field already carries.
 */
const ACTIVE_BATCH_STATUSES: readonly string[] = ['pending', 'submitting']

/** How many recent batches the orders panel lists; the Orders page is where history lives. */
const RECENT_BATCHES = 5

/** How much of a saved explanation the panel shows before deferring to the Backtest page. */
const EXPLANATION_EXCERPT_CHARS = 320

/** One source's state.
 *
 * `stale` is the case that makes this a union rather than a boolean: a refresh that failed has
 * not made what is on screen wrong, so the previous values stay and the failure is reported
 * beside them rather than in place of them.
 */
type Section<T> =
  | { phase: 'loading' }
  | { phase: 'ready'; data: T }
  | { phase: 'stale'; data: T; error: string }
  | { phase: 'error'; message: string }

type Loaded<T> = Extract<Section<T>, { data: T }>

function hasData<T>(section: Section<T>): section is Loaded<T> {
  return section.phase === 'ready' || section.phase === 'stale'
}

function aMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : 'Something went wrong.'
}

/** What one source's outcome does to its section: the value, or the failure kept beside it. */
function nextSection<T>(previous: Section<T>, outcome: PromiseSettledResult<T>): Section<T> {
  if (outcome.status === 'fulfilled') return { phase: 'ready', data: outcome.value }
  const message = aMessage(outcome.reason)
  return hasData(previous)
    ? { phase: 'stale', data: previous.data, error: message }
    : { phase: 'error', message }
}

/** How the prices behind a valuation should be described.
 *
 * Which one this is matters more than any figure on the page: a fictional demo balance presented
 * as a real one is the most misleading thing a portfolio summary can do.
 */
function priceSourceNote(source: string): string {
  return source === 'demo'
    ? 'Fictional demo prices, not market data.'
    : `${source} — paper trading, not a live account.`
}

/** The first part of a saved explanation, cut at a word boundary.
 *
 * An excerpt of stored text, not a summary of it: nothing is rewritten, reordered or scored.
 */
function excerpt(text: string): string {
  const first = text.split('\n\n')[0].trim()
  if (first.length <= EXPLANATION_EXCERPT_CHARS) return first
  const cut = first.slice(0, EXPLANATION_EXCERPT_CHARS)
  const at = cut.lastIndexOf(' ')
  return `${at > 0 ? cut.slice(0, at) : cut}…`
}

function Badge({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-full bg-selected px-2.5 py-1 text-label text-accent-ink">
      {children}
    </span>
  )
}

/** Why a section is showing values it did not just load, or null when it is current.
 *
 * One refresh is one operation, so its failure is reported once at the top of the page rather
 * than repeated under every surface the failed source feeds -- the valuation alone feeds three,
 * and the same sentence three times is noise rather than information.
 */
function staleReason(section: Section<unknown>): string | null {
  return section.phase === 'stale' ? section.error : null
}

/** A section that could not be read: what is missing, why, and a way to ask again. */
function Unavailable({
  what,
  message,
  onRetry,
}: {
  what: string
  message: string
  onRetry: () => void
}) {
  return (
    <div className="mt-2">
      <p className="text-figure font-semibold text-dim">{PLACEHOLDER}</p>
      <p className="mt-1 text-label text-faint">
        {what} could not be read. {message}
      </p>
      <Button className="mt-2" onClick={onRetry}>
        Retry
      </Button>
    </div>
  )
}

/** A card whose body is one figure from one source, in whichever of the four states it is in. */
function FigureCard<T>({
  label,
  section,
  what,
  onRetry,
  value,
  note,
  badge,
}: {
  label: string
  section: Section<T>
  what: string
  onRetry: () => void
  value: (data: T) => ReactNode
  note?: (data: T) => ReactNode
  badge?: string
}) {
  return (
    <Card className="flex flex-col">
      <div className="flex items-start justify-between gap-2">
        <h2 className="text-label font-medium text-dim">{label}</h2>
        {badge !== undefined && <Badge>{badge}</Badge>}
      </div>

      {section.phase === 'loading' && (
        <p className="mt-2 text-figure font-semibold text-dim" role="status">
          Loading…
        </p>
      )}

      {section.phase === 'error' && (
        <Unavailable what={what} message={section.message} onRetry={onRetry} />
      )}

      {hasData(section) && (
        <>
          <p className="mt-2 text-figure font-semibold tabular-nums text-ink">
            {value(section.data)}
          </p>
          {note !== undefined && (
            <p className="mt-1 text-label text-faint">{note(section.data)}</p>
          )}
        </>
      )}
    </Card>
  )
}

export function DashboardPage({
  onNavigate,
  onOpenOrders,
}: {
  onNavigate: (page: PageId) => void
  onOpenOrders?: (batchId: string) => void
}) {
  const [valuation, setValuation] = useState<Section<Valuation>>({ phase: 'loading' })
  const [orders, setOrders] = useState<Section<OrdersView>>({ phase: 'loading' })
  const [backtest, setBacktest] = useState<Section<BacktestRun | null>>({ phase: 'loading' })
  const [analysis, setAnalysis] = useState<Section<LatestAnalysis | null>>({ phase: 'loading' })
  const [refreshing, setRefreshing] = useState(false)

  // The request in flight. A ref because neither the guard nor the staleness check is rendered,
  // and because both have to be decided synchronously: a re-render is not a synchronisation
  // primitive, and a duplicate Refresh must be dropped before it is sent, not after.
  const inFlight = useRef<AbortController | null>(null)

  const load = useCallback(async () => {
    // A duplicate Refresh asks for exactly what the request already in flight is asking for.
    if (inFlight.current !== null) return

    const controller = new AbortController()
    inFlight.current = controller
    setRefreshing(true)

    // All four at once, and `allSettled` rather than `all`: one source failing must not stop the
    // others being applied. Each `.then` unwraps the response to the thing its section holds, so
    // the backtest section holds a run and not a response body.
    const outcomes = await Promise.allSettled([
      fetchValuation(controller.signal),
      fetchOrders(controller.signal),
      fetchLatestBacktestRun(controller.signal).then((body) => body.run),
      fetchLatestAnalysis(controller.signal).then((body) => body.analysis),
    ])

    // A response that arrived after this component abandoned its request belongs to a state that
    // no longer exists. Checked once, before anything is applied, rather than in each branch.
    if (inFlight.current !== controller) return
    inFlight.current = null
    setRefreshing(false)

    const [portfolio, batches, run, latest] = outcomes
    setValuation((previous) => nextSection(previous, portfolio))
    setOrders((previous) => nextSection(previous, batches))
    setBacktest((previous) => nextSection(previous, run))
    setAnalysis((previous) => nextSection(previous, latest))
  }, [])

  useEffect(() => {
    void load()
    return () => {
      // Abandoning the request and *then* clearing the ref is what makes the guard above reject
      // the outcomes: on unmount, and on StrictMode's double mount, the state of a request nobody
      // is waiting for is never written.
      inFlight.current?.abort()
      inFlight.current = null
    }
  }, [load])

  const retry = useCallback(() => {
    void load()
  }, [load])

  // Which sources are showing older values than they should be, by name. Derived from the same
  // sections the page renders, so the note cannot describe a state the page is not in.
  const stale = (
    [
      ['the portfolio valuation', valuation],
      ['the execution batches', orders],
      ['the saved backtests', backtest],
      ['the saved analyses', analysis],
    ] as Array<[string, Section<unknown>]>
  )
    .map(([label, section]) => {
      const reason = staleReason(section)
      return reason === null ? null : `${label} (${reason})`
    })
    .filter((line): line is string => line !== null)

  return (
    <div>
      <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div>
          <h1 className="text-title font-bold text-ink">Dashboard</h1>
          <p className="mt-1 text-note text-dim">
            Your portfolio, signals and strategy research in one place.
          </p>
        </div>
        {/* `aria-disabled` rather than `disabled`, so the button keeps focus and a keyboard user
            is not dropped back to the top of the page while a refresh runs. The in-flight guard
            is what actually prevents the second request. */}
        <Button aria-disabled={refreshing} onClick={retry}>
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </Button>
      </header>

      <p className="sr-only" role="status">
        {refreshing ? 'Refreshing dashboard data' : ''}
      </p>

      {stale.length > 0 && (
        <div className="mt-4">
          <Banner>
            Some of this could not be refreshed, so what is shown is the last data that loaded:{' '}
            {stale.join('; ')}.{' '}
            <button type="button" className="underline" onClick={retry}>
              Try again
            </button>
          </Banner>
        </div>
      )}

      <div className="mt-6 grid grid-cols-1 gap-4 min-[760px]:grid-cols-2 min-[1500px]:grid-cols-4">
        <PortfolioValueCard section={valuation} onRetry={retry} />
        <FigureCard
          label="Cash"
          section={valuation}
          what="The portfolio valuation"
          onRetry={retry}
          // Named "Cash" and not "buying power": this is the account's settled balance, not what
          // a broker would lend against it.
          note={() => 'Settled cash balance, not broker buying power.'}
          value={(portfolio) => formatCurrency(portfolio.cash_balance)}
        />
        <FigureCard
          label="Active order batches"
          section={orders}
          what="The execution batches"
          onRetry={retry}
          value={(view) => String(activeBatches(view))}
          note={(view) =>
            activeBatches(view) === 0
              ? `No batch is working. ${view.batches.length} recorded in total.`
              : `Still working, out of ${view.batches.length} recorded.`
          }
        />
        <FigureCard
          label="Latest backtest return"
          section={backtest}
          what="The saved backtests"
          onRetry={retry}
          badge="Simulated"
          value={(run) =>
            run?.performance == null
              ? PLACEHOLDER
              : formatSignedPercent(run.performance.total_return_percent)
          }
          note={(run) =>
            run === null
              ? 'No completed backtest is saved yet.'
              : `${run.symbol} · ${run.strategy_name} v${run.strategy_version} · ${run.start_date} to ${run.end_date}`
          }
        />
      </div>

      {/* `items-start`, so the allocation card ends where its legend ends rather than being
          stretched to the chart's height with a bordered void beneath it. */}
      <div className="mt-4 grid grid-cols-1 items-start gap-4 min-[1100px]:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <BacktestSection section={backtest} onRetry={retry} onNavigate={onNavigate} />
        <AllocationSection section={valuation} onRetry={retry} onNavigate={onNavigate} />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 min-[1100px]:grid-cols-3">
        <AnalysisPanel section={analysis} onRetry={retry} onNavigate={onNavigate} />
        <OrdersPanel
          section={orders}
          onRetry={retry}
          onNavigate={onNavigate}
          onOpenOrders={onOpenOrders}
        />
        <ExplanationPanel section={backtest} onNavigate={onNavigate} />
      </div>
    </div>
  )
}

/** How many of the recorded batches are still working, by the application's own definition. */
function activeBatches(view: OrdersView): number {
  return view.batches.filter((batch) => ACTIVE_BATCH_STATUSES.includes(batch.status)).length
}

function PortfolioValueCard({
  section,
  onRetry,
}: {
  section: Section<Valuation>
  onRetry: () => void
}) {
  return (
    <FigureCard
      label="Portfolio value"
      section={section}
      what="The portfolio valuation"
      onRetry={onRetry}
      value={(portfolio) => formatCurrency(portfolio.total_value)}
      note={(portfolio) =>
        `${priceSourceNote(portfolio.price_source)} ${
          portfolio.last_synced_at === null
            ? 'Never synced from a broker.'
            : `Last synced ${formatDateTime(portfolio.last_synced_at) ?? PLACEHOLDER}.`
        }`
      }
    />
  )
}

/** The latest saved backtest: its curves, and what they were over. */
function BacktestSection({
  section,
  onRetry,
  onNavigate,
}: {
  section: Section<BacktestRun | null>
  onRetry: () => void
  onNavigate: (page: PageId) => void
}) {
  if (section.phase === 'loading') {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Latest backtest</h2>
        <p className="mt-3 text-body text-dim" role="status">
          Loading the last saved run…
        </p>
      </Card>
    )
  }

  if (section.phase === 'error') {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Latest backtest</h2>
        <div className="mt-3">
          <ErrorNotice
            title="Could not read the saved backtests."
            message={section.message}
            onRetry={onRetry}
          />
        </div>
      </Card>
    )
  }

  const run = section.data
  if (run === null) {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Latest backtest</h2>
        <p className="mt-3 text-body text-dim">
          No backtest has been saved yet. Running one tests a strategy against a stock&rsquo;s past
          prices &mdash; nothing is traded.
        </p>
        <Button className="mt-3" onClick={() => onNavigate('backtest')}>
          Open backtest
        </Button>
      </Card>
    )
  }

  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h2 className="text-label font-medium text-dim">Latest backtest</h2>
        <Badge>Simulated</Badge>
      </div>

      <p className="mt-2 text-note text-dim">
        <span className="font-medium text-ink">{run.symbol}</span> · {run.strategy_name} v
        {run.strategy_version} · {run.start_date} to {run.end_date}
      </p>
      <p className="mt-1 text-label text-faint">
        A historical simulation over past prices. Nothing here was traded.
      </p>

      {run.equity.length > 0 && (
        <div className="mt-4">
          <EquityChart
            equity={run.equity}
            benchmark={run.benchmark_equity}
            strategyName={run.strategy_name}
          />
        </div>
      )}

      <Button className="mt-3" onClick={() => onNavigate('backtest')}>
        Open backtest
      </Button>

    </Card>
  )
}

/** Allocation from the same valuation the summary cards read. */
function AllocationSection({
  section,
  onRetry,
  onNavigate,
}: {
  section: Section<Valuation>
  onRetry: () => void
  onNavigate: (page: PageId) => void
}) {
  if (section.phase === 'loading') {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Allocation</h2>
        <p className="mt-3 text-body text-dim" role="status">
          Loading the portfolio valuation…
        </p>
      </Card>
    )
  }

  if (section.phase === 'error') {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Allocation</h2>
        <div className="mt-3">
          <ErrorNotice
            title="Could not read the portfolio valuation."
            message={section.message}
            onRetry={onRetry}
          />
        </div>
      </Card>
    )
  }

  const portfolio = section.data
  const assets = allocationAssets(
    portfolio.holdings,
    portfolio.cash_allocation_percent,
    portfolio.cash_balance,
  )

  // A negative balance or holding makes a share of the total meaningless: the segments would have
  // to be clamped or the percentages invented, and both would misstate the portfolio. The amounts
  // are listed as they are, and the ring is not drawn.
  const negative =
    portfolio.cash_balance.startsWith('-') ||
    portfolio.holdings.some((holding) => holding.holding_value.startsWith('-'))

  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h2 className="text-label font-medium text-dim">Allocation</h2>
        <Button onClick={() => onNavigate('portfolio')}>Open portfolio</Button>
      </div>

      {negative ? (
        <div className="mt-3">
          <ul className="space-y-1.5 text-label">
            {assets.map((asset) => (
              <li key={asset.key} className="flex items-center gap-2">
                <span className="text-dim">{asset.label}</span>
                <span className="ml-auto tabular-nums text-ink">
                  {asset.value === undefined || asset.value === null
                    ? PLACEHOLDER
                    : formatCurrency(asset.value)}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-label text-faint">
            No share of the total is shown, because a negative balance in the portfolio makes a
            percentage of it misleading. These are the portfolio&rsquo;s own amounts.
          </p>
        </div>
      ) : (
        <div className="mt-3">
          <AllocationDonut
            label="Share of portfolio value"
            assets={assets}
            caption={`Total ${formatCurrency(portfolio.total_value)}`}
          />
          {portfolio.holdings.length === 0 && !nothingToAllocate(assets) && (
            <p className="mt-2 text-label text-faint">
              No stock is held, so the portfolio is entirely cash.
            </p>
          )}
        </div>
      )}

    </Card>
  )
}

/** The latest completed Module 1 analysis, from the fields that actually exist. */
function AnalysisPanel({
  section,
  onRetry,
  onNavigate,
}: {
  section: Section<LatestAnalysis | null>
  onRetry: () => void
  onNavigate: (page: PageId) => void
}) {
  if (section.phase === 'loading') {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Insider analysis</h2>
        <p className="mt-3 text-body text-dim" role="status">
          Loading the last saved analysis…
        </p>
      </Card>
    )
  }

  if (section.phase === 'error') {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Insider analysis</h2>
        <div className="mt-3">
          <ErrorNotice
            title="Could not read the saved analyses."
            message={section.message}
            onRetry={onRetry}
          />
        </div>
      </Card>
    )
  }

  const saved = section.data
  if (saved === null) {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Insider analysis</h2>
        <p className="mt-3 text-body text-dim">
          No analysis has been completed yet. One compares a company&rsquo;s price movement with
          its reported insider activity.
        </p>
        <Button className="mt-3" onClick={() => onNavigate('analysis')}>
          Open Analysis
        </Button>
      </Card>
    )
  }

  const window =
    saved.start_date !== null && saved.end_date !== null
      ? `${saved.start_date} to ${saved.end_date}`
      : saved.as_of

  return (
    <Card>
      <h2 className="text-label font-medium text-dim">Insider analysis</h2>

      <p className="mt-2 text-heading font-medium text-ink">
        {saved.symbol ?? 'No instrument recorded'}
      </p>
      <p className="mt-1 text-note text-dim">
        {saved.destination === 'module1_analysis'
          ? 'Price movement against reported insider activity.'
          : 'A stored analysis.'}
      </p>

      <dl className="mt-3 space-y-1 text-label">
        <Row label="Status" value={saved.status} />
        <Row label="Window" value={window ?? PLACEHOLDER} />
        <Row
          label="Completed"
          value={
            saved.completed_at === null
              ? PLACEHOLDER
              : (formatDateTime(saved.completed_at) ?? PLACEHOLDER)
          }
        />
      </dl>

      {saved.question !== null && (
        <p className="mt-3 text-label text-faint">Asked: {saved.question}</p>
      )}

      {/* Said rather than left to be assumed. An analysis in this system carries no score, and a
          summary that implied one by staying silent about it would be inventing it. */}
      <p className="mt-2 text-label text-faint">
        This system reports no signal score or confidence for an analysis.
      </p>

      <Button className="mt-3" onClick={() => onNavigate('analysis')}>
        Open Analysis
      </Button>

    </Card>
  )
}

/** Recent execution batches, as the Orders page records them. */
function OrdersPanel({
  section,
  onRetry,
  onNavigate,
  onOpenOrders,
}: {
  section: Section<OrdersView>
  onRetry: () => void
  onNavigate: (page: PageId) => void
  onOpenOrders?: (batchId: string) => void
}) {
  if (section.phase === 'loading') {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Paper orders</h2>
        <p className="mt-3 text-body text-dim" role="status">
          Loading the execution batches…
        </p>
      </Card>
    )
  }

  if (section.phase === 'error') {
    return (
      <Card>
        <h2 className="text-label font-medium text-dim">Paper orders</h2>
        <div className="mt-3">
          <ErrorNotice
            title="Could not read the execution batches."
            message={section.message}
            onRetry={onRetry}
          />
        </div>
      </Card>
    )
  }

  const batches = section.data.batches.slice(0, RECENT_BATCHES)

  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h2 className="text-label font-medium text-dim">Paper orders</h2>
        <Button onClick={() => onNavigate('orders')}>View orders</Button>
      </div>
      <p className="mt-1 text-label text-faint">Sent to the Alpaca paper account.</p>

      {batches.length === 0 ? (
        <p className="mt-3 text-body text-dim">
          No orders have been submitted. Approving a rebalance proposal is what creates a batch.
        </p>
      ) : (
        <ul className="mt-3 space-y-2">
          {batches.map((batch) => (
            <BatchRow key={batch.batch_id} batch={batch} onOpen={onOpenOrders} />
          ))}
        </ul>
      )}

    </Card>
  )
}

function BatchRow({
  batch,
  onOpen,
}: {
  batch: ExecutionBatch
  onOpen?: (batchId: string) => void
}) {
  const filled = batch.orders.filter((order) => order.status === 'filled').length
  return (
    <li className="border-t border-line pt-2 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center gap-x-2">
        <span className="text-body text-ink">{batch.status}</span>
        {ACTIVE_BATCH_STATUSES.includes(batch.status) && <Badge>working</Badge>}
        <span className="text-label text-faint">
          {batch.orders.length} order{batch.orders.length === 1 ? '' : 's'}, {filled} filled
        </span>
        {onOpen !== undefined && (
          // The Orders page can open focused on one batch; this reuses that rather than sending a
          // reader to a list to find it themselves.
          <button
            type="button"
            className="ml-auto text-label text-accent-ink underline"
            onClick={() => onOpen(batch.batch_id)}
          >
            Open
          </button>
        )}
      </div>
      <p className="mt-0.5 text-label text-faint">
        {formatDateTime(batch.created_at) ?? PLACEHOLDER}
        {batch.completed_at === null ? '' : ` · completed ${formatDateTime(batch.completed_at)}`}
      </p>
    </li>
  )
}

/** An excerpt of the explanation saved for the run the chart above is showing. */
function ExplanationPanel({
  section,
  onNavigate,
}: {
  section: Section<BacktestRun | null>
  onNavigate: (page: PageId) => void
}) {
  // Nothing is asked of a model here. Reading the explanation the run already carries is the whole
  // panel; writing one is the Backtest page's job, and this says where to go for it.
  const run = hasData(section) ? section.data : null
  const explanation = run?.explanation ?? null

  return (
    <Card>
      <h2 className="text-label font-medium text-dim">Backtest risk explanation</h2>

      {explanation === null ? (
        <>
          <p className="mt-3 text-body text-dim">No explanation saved for this run.</p>
          <Button className="mt-3" onClick={() => onNavigate('backtest')}>
            Open backtest to explain
          </Button>
        </>
      ) : (
        <>
          <p className="mt-2 text-note text-dim">{excerpt(explanation)}</p>
          <p className="mt-2 text-label text-faint">
            Written by a model
            {run?.explanation_model == null ? '' : ` (${run.explanation_model})`} from this
            run&rsquo;s own figures. Prose, not a calculation.
          </p>
          {/* No button here: the chart card above already opens the Backtest page, and two
              controls with one accessible name are ambiguous to anyone navigating by name. */}
        </>
      )}
    </Card>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="text-faint">{label}</dt>
      <dd className="ml-auto text-ink">{value}</dd>
    </div>
  )
}
