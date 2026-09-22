/** The What if? page: a scenario form and its results.
 *
 * The form's stock list and every figure in the results panel come from the backend.
 * This page does no financial arithmetic — it sends a symbol and a percentage, and
 * prints what comes back. The percentage goes out as a *string* so it reaches the
 * server's `Decimal` without passing through a float.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError, fetchScenario, fetchValuation } from '../api'
import type { Scenario, Valuation, ValuationHolding } from '../api'
import {
  directionOf,
  formatCurrency,
  formatPercent,
  formatSignedPercent,
  formatSyncTime,
} from '../format'
import { Button } from './Button'
import { Card } from './Card'
import { ErrorNotice, LoadingCards } from './States'

type State =
  | { phase: 'loading' }
  | { phase: 'ready'; valuation: Valuation }
  | { phase: 'error'; message: string; portfolioMissing: boolean }

/** Separate from `State`: the holdings load once, but the scenario runs on demand. */
type Result =
  | { phase: 'idle' }
  | { phase: 'calculating' }
  | { phase: 'ready'; scenario: Scenario }
  | { phase: 'error'; message: string }

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}

export function WhatIfPage() {
  const [state, setState] = useState<State>({ phase: 'loading' })
  const [chosen, setChosen] = useState<string | null>(null)
  const [percent, setPercent] = useState('0')
  const [result, setResult] = useState<Result>({ phase: 'idle' })

  const load = useCallback(async (signal?: AbortSignal) => {
    setState({ phase: 'loading' })

    try {
      const valuation = await fetchValuation(signal)
      setState({ phase: 'ready', valuation })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setState({
        phase: 'error',
        message: messageOf(error),
        portfolioMissing: error instanceof ApiError && error.status === 404,
      })
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const holdings = state.phase === 'ready' ? state.valuation.holdings : []
  // Falls back to the first holding, so the select is never blank once data arrives.
  const symbol = chosen ?? holdings[0]?.symbol ?? ''

  const calculate = useCallback(async () => {
    const trimmed = percent.trim()

    // Only a range check. The percentage is not a money amount, so parsing it with
    // Number here costs nothing — it is never what gets sent or displayed.
    if (trimmed === '' || Number.isNaN(Number(trimmed))) {
      setResult({ phase: 'error', message: 'Enter a percentage to explore.' })
      return
    }
    if (Number(trimmed) < -100 || Number(trimmed) > 100) {
      setResult({ phase: 'error', message: 'Enter a percentage between -100 and 100.' })
      return
    }

    setResult({ phase: 'calculating' })
    try {
      const scenario = await fetchScenario({
        symbol,
        price_change_percent: trimmed,
      })
      setResult({ phase: 'ready', scenario })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setResult({ phase: 'error', message: messageOf(error) })
    }
  }, [percent, symbol])

  // Any change to the inputs invalidates the figures already on screen: leaving NVDA's
  // result visible under a form that now says AAPL would be a lie of omission.
  function chooseSymbol(next: string) {
    setChosen(next)
    setResult({ phase: 'idle' })
  }

  function changePercent(next: string) {
    setPercent(next)
    setResult({ phase: 'idle' })
  }

  return (
    <div>
      <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div>
          <h1 className="text-page font-bold text-ink">What if?</h1>
          <p className="mt-1 text-note text-dim">
            Explore how a hypothetical stock price change could affect your portfolio.
          </p>
        </div>
        <PriceBadge state={state} />
      </header>

      <div className="mt-6">
        {state.phase === 'loading' && <LoadingCards />}

        {state.phase === 'error' && (
          <ErrorNotice
            title={
              state.portfolioMissing
                ? 'No portfolio available.'
                : 'Could not load the portfolio.'
            }
            message={state.message}
            onRetry={() => void load()}
          />
        )}

        {state.phase === 'ready' && (
          // The same 40/60 grid the Portfolio page uses: side by side on desktop,
          // stacked on mobile.
          <div className="grid grid-cols-1 gap-4 min-[1100px]:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <ScenarioInputs
              holdings={holdings}
              symbol={symbol}
              onSymbolChange={chooseSymbol}
              percent={percent}
              onPercentChange={changePercent}
              onCalculate={calculate}
              calculating={result.phase === 'calculating'}
            />
            <ImpactCard result={result} />
          </div>
        )}
      </div>
    </div>
  )
}

/** The same claim the Portfolio page makes about its figures, made here.
 *
 * This page shows no badge until it has data, because "no badge" would read as "these are
 * live prices" — and the form below is only usable once the holdings have loaded anyway.
 */
function PriceBadge({ state }: { state: State }) {
  if (state.phase !== 'ready') return null

  const { price_source: source, last_synced_at: lastSyncedAt } = state.valuation
  if (source === 'demo') {
    return (
      <span className="rounded-full bg-selected px-2.5 py-1 text-label text-accent-ink">
        Demo prices
      </span>
    )
  }

  const syncedAt = formatSyncTime(lastSyncedAt)
  return (
    <span className="rounded-full bg-selected px-2.5 py-1 text-label text-accent-ink">
      Alpaca Paper
      {syncedAt === null ? ' · Not synced yet' : ` · Last synced ${syncedAt}`}
    </span>
  )
}

function ScenarioInputs({
  holdings,
  symbol,
  onSymbolChange,
  percent,
  onPercentChange,
  onCalculate,
  calculating,
}: {
  holdings: ValuationHolding[]
  symbol: string
  onSymbolChange: (symbol: string) => void
  percent: string
  onPercentChange: (percent: string) => void
  onCalculate: () => void
  calculating: boolean
}) {
  // A cash-only portfolio has no stock to model. The API still returns a portfolio, so
  // this is a real state rather than a defensive one.
  if (holdings.length === 0) {
    return (
      <Card className="flex flex-col min-[1100px]:min-h-[400px]">
        <h2 className="text-heading font-medium text-ink">Scenario inputs</h2>
        <p className="mt-3 text-body text-dim">No stock holdings to model.</p>
      </Card>
    )
  }

  return (
    <Card className="flex flex-col min-[1100px]:min-h-[400px]">
      <h2 className="text-heading font-medium text-ink">Scenario inputs</h2>

      <div className="mt-5 space-y-5">
        <div>
          <label htmlFor="what-if-symbol" className="text-label text-faint">
            Stock
          </label>
          <select
            id="what-if-symbol"
            value={symbol}
            onChange={(event) => onSymbolChange(event.target.value)}
            className="mt-1.5 h-9 w-full rounded-md border border-line bg-page px-3 text-body text-ink"
          >
            {/* Holdings only: cash is not a stock and has no price to move. */}
            {holdings.map((holding) => (
              <option key={holding.symbol} value={holding.symbol}>
                {holding.symbol}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="what-if-percent" className="text-label text-faint">
            Price change
          </label>
          <div className="mt-1.5 flex items-center gap-2">
            <input
              id="what-if-percent"
              type="number"
              min={-100}
              max={100}
              step={1}
              value={percent}
              onChange={(event) => onPercentChange(event.target.value)}
              className="h-9 w-28 rounded-md border border-line bg-page px-3 text-right tabular-nums text-body text-ink"
            />
            <span className="text-body text-dim">%</span>
          </div>
          <p className="mt-1.5 text-label text-faint">
            Use a negative number for a price drop and a positive number for an increase.
          </p>
        </div>
      </div>

      <div className="mt-6">
        {/* aria-disabled rather than disabled: a button that becomes `disabled` while
            focused drops the focus, and the click guard already prevents a second run. */}
        <Button
          variant="primary"
          onClick={() => {
            if (!calculating) onCalculate()
          }}
          aria-disabled={calculating}
          className={calculating ? 'opacity-50' : ''}
        >
          {calculating ? 'Calculating…' : 'Calculate impact'}
        </Button>
      </div>

      <p className="mt-auto pt-5 text-label text-faint">
        Other asset prices, cash, and quantities remain unchanged.
      </p>
    </Card>
  )
}

function ImpactCard({ result }: { result: Result }) {
  return (
    <Card className="flex flex-col min-[1100px]:min-h-[400px]">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h2 className="text-heading font-medium text-ink">Portfolio impact</h2>
        {/* Only ever shown next to figures, and it stays on screen with them. */}
        {result.phase === 'ready' && (
          <span className="rounded-full bg-selected px-2.5 py-1 text-label text-accent-ink">
            Hypothetical
          </span>
        )}
      </div>

      {result.phase === 'idle' && (
        <p className="mt-3 text-body text-dim">
          Choose a stock and enter a price change to explore its impact.
        </p>
      )}

      {result.phase === 'calculating' && (
        <p className="mt-3 text-body text-dim" role="status">
          Calculating…
        </p>
      )}

      {result.phase === 'error' && (
        <p role="alert" className="mt-3 text-body text-danger">
          {result.message}
        </p>
      )}

      {result.phase === 'ready' && <ScenarioResult scenario={result.scenario} />}
    </Card>
  )
}

function ScenarioResult({ scenario }: { scenario: Scenario }) {
  const direction = directionOf(scenario.change_value)
  const tone =
    direction === 'decrease'
      ? 'text-danger'
      : direction === 'increase'
        ? 'text-positive'
        : 'text-ink'

  return (
    <div className="mt-4">
      <p className="text-note text-dim">
        <span className="font-medium text-ink">{scenario.symbol}</span>{' '}
        {formatSignedPercent(scenario.price_change_percent)}
      </p>

      <dl className="mt-4 space-y-3">
        <ChangeRow
          label={`${scenario.symbol} price`}
          before={formatCurrency(scenario.price_before)}
          after={formatCurrency(scenario.price_after)}
        />
        <ChangeRow
          label="Holding value"
          before={formatCurrency(scenario.holding_value_before)}
          after={formatCurrency(scenario.holding_value_after)}
        />
        <ChangeRow
          label="Portfolio value"
          before={formatCurrency(scenario.total_value_before)}
          after={formatCurrency(scenario.total_value_after)}
        />
      </dl>

      <div className="mt-5 border-t border-line pt-4">
        <p className="text-label text-faint">Change</p>
        {/* Three signals for the same thing: the sign, the colour, and the word. The
            word is the one that still works in greyscale or through a screen reader. */}
        <p className={`mt-1 tabular-nums text-heading font-medium ${tone}`}>
          {formatCurrency(scenario.change_value)}
          {scenario.change_percent !== null &&
            ` (${formatPercent(scenario.change_percent)})`}
          {` — ${direction}`}
        </p>
      </div>
    </div>
  )
}

function ChangeRow({
  label,
  before,
  after,
}: {
  label: string
  before: string
  after: string
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3">
      <dt className="text-note text-dim">{label}</dt>
      <dd className="tabular-nums text-body text-ink">
        <span className="text-faint">{before}</span>
        <span className="mx-1.5 text-faint" aria-hidden="true">
          →
        </span>
        {/* The arrow is decorative; this is what a screen reader reads instead. */}
        <span className="sr-only">becomes</span>
        {after}
      </dd>
    </div>
  )
}
