import { useCallback, useEffect, useState } from 'react'

import { ApiError, fetchValuation } from '../api'
import type { Valuation } from '../api'
import { Button } from './Button'
import { HoldingsCard } from './HoldingsCard'
import { PortfolioValueCard } from './PortfolioValueCard'
import { Banner, ErrorNotice, LoadingCards } from './States'

/** The sections the page will eventually have. Only Overview exists today. */
const TABS = ['Overview', 'Holdings', 'Performance', 'Allocation', 'History']

/** One state at a time, so "loading and also showing data" is unrepresentable.
 *
 * `stale` is the important one: a refresh failed but the previous numbers are still
 * on screen. They are kept, and labelled, rather than cleared or passed off as fresh.
 */
type State =
  | { phase: 'loading' }
  | { phase: 'ready'; valuation: Valuation }
  | { phase: 'stale'; valuation: Valuation; message: string }
  | { phase: 'error'; message: string; portfolioMissing: boolean }

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}

function RefreshIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" />
      <path d="M13.5 2.5V5H11" />
    </svg>
  )
}

export function PortfolioPage() {
  const [state, setState] = useState<State>({ phase: 'loading' })
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    // Anything already on screen stays on screen while this runs; only a page with
    // nothing to show falls back to the skeletons.
    setRefreshing(true)

    try {
      const valuation = await fetchValuation(signal)
      setState({ phase: 'ready', valuation })
    } catch (error) {
      // An aborted request was replaced by a newer one, or the page is going away.
      if (error instanceof DOMException && error.name === 'AbortError') return

      const message = messageOf(error)
      setState((current) =>
        current.phase === 'ready' || current.phase === 'stale'
          ? { phase: 'stale', valuation: current.valuation, message }
          : {
              phase: 'error',
              message,
              portfolioMissing: error instanceof ApiError && error.status === 404,
            },
      )
    } finally {
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    // StrictMode runs this twice in development; aborting the first request keeps the
    // two from racing to set state.
    return () => controller.abort()
  }, [load])

  return (
    <div>
      <header className="flex items-start justify-between gap-4">
        <h1 className="text-page font-bold text-ink">Portfolio</h1>

        {/* aria-disabled rather than disabled: a button that becomes `disabled` while
            focused drops the focus, and a keyboard user would have to find their way
            back after every refresh. The click guard does the same job without that. */}
        <Button
          onClick={() => {
            if (!refreshing) void load()
          }}
          aria-disabled={refreshing}
          className={refreshing ? 'opacity-50' : ''}
        >
          <RefreshIcon />
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </Button>
      </header>

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line">
        <div role="tablist" aria-label="Portfolio sections" className="flex gap-1">
          {TABS.map((tab, index) => {
            const active = index === 0
            return (
              <button
                key={tab}
                id={`portfolio-tab-${index}`}
                type="button"
                role="tab"
                aria-selected={active}
                aria-controls={active ? 'portfolio-overview' : undefined}
                disabled={!active}
                title={active ? undefined : 'Coming soon'}
                // -mb-px pulls the 2px underline down over the row's own divider, so
                // the active tab looks like it interrupts the line rather than
                // sitting above it.
                className={`-mb-px border-b-2 px-3 py-2 text-body transition-colors ${
                  active
                    ? 'border-accent font-medium text-accent-ink'
                    : 'border-transparent text-faint disabled:opacity-70'
                }`}
              >
                {tab}
              </button>
            )
          })}
        </div>

        <span className="mb-2 ml-auto rounded-full bg-selected px-2.5 py-1 text-label text-accent-ink">
          Demo prices
        </span>
      </div>

      <div
        id="portfolio-overview"
        role="tabpanel"
        aria-labelledby="portfolio-tab-0"
        aria-busy={refreshing}
        className="mt-6"
      >
        <Overview state={state} onRetry={() => void load()} />
      </div>
    </div>
  )
}

function Overview({ state, onRetry }: { state: State; onRetry: () => void }) {
  if (state.phase === 'loading') return <LoadingCards />

  if (state.phase === 'error') {
    return (
      <ErrorNotice
        title={
          state.portfolioMissing ? 'No portfolio available.' : 'Could not load the portfolio.'
        }
        // For a 404 this is the backend's own text, which names the exact seed command
        // that fixes it.
        message={state.message}
        onRetry={onRetry}
      />
    )
  }

  return (
    <div className="space-y-4">
      {state.phase === 'stale' && (
        <Banner>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <span>Showing previously loaded values. {state.message}</span>
            <Button className="bg-transparent" onClick={onRetry}>
              Retry
            </Button>
          </div>
        </Banner>
      )}

      {/* minmax(0, …) rather than a plain fr: an fr track will not shrink below its
          content's min-content width, which would let the 520px-wide table push the
          whole page sideways instead of scrolling inside its own card. */}
      <div className="grid grid-cols-1 gap-4 min-[1100px]:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <PortfolioValueCard valuation={state.valuation} />
        <HoldingsCard valuation={state.valuation} />
      </div>
    </div>
  )
}
