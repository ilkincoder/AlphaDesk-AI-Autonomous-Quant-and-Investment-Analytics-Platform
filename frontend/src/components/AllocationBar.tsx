/** Allocation, as one bar plus a legend.
 *
 * Every percentage shown here comes from the API. The browser multiplies nothing: a
 * segment's width is the string the backend sent, used as a CSS percentage, and a
 * legend entry is that same string printed. That is why the segments need not add to
 * exactly 100 — Day 3 rounds each percentage independently and says so.
 */

import { formatPercent } from '../format'
import type { ValuationHolding } from '../api'

/** Purple shades for holdings, in API order. Cycled, so a portfolio with more
 *  holdings than shades repeats rather than running out and rendering nothing. */
const HOLDING_SHADES = ['#8B5CF6', '#A78BFA', '#7C3AED', '#6D28D9', '#C4B5FD']
const CASH_SHADE = 'var(--color-cash)'

export function AllocationBar({
  holdings,
  cashAllocationPercent,
}: {
  holdings: ValuationHolding[]
  cashAllocationPercent: string | null
}) {
  const segments = [
    ...holdings.map((holding, index) => ({
      key: holding.symbol,
      label: holding.symbol,
      percent: holding.allocation_percent,
      colour: HOLDING_SHADES[index % HOLDING_SHADES.length],
    })),
    {
      key: 'CASH',
      label: 'Cash',
      percent: cashAllocationPercent,
      colour: CASH_SHADE,
    },
  ]

  // A zero-value portfolio has no denominator, so the API sends null for every
  // percentage. Drawing an empty bar with no explanation would look like a bug.
  const nothingToShow = segments.every((segment) => segment.percent === null)
  if (nothingToShow) {
    return (
      <div>
        <div className="h-2 w-full rounded-full bg-line" aria-hidden="true" />
        <p className="mt-3 text-note text-faint">
          Allocation unavailable for a zero-value portfolio.
        </p>
      </div>
    )
  }

  return (
    <div>
      {/* Decorative: the legend below carries the same information as text, which is
          what a screen reader should read. */}
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-line" aria-hidden="true">
        {segments.map((segment) => (
          <div
            key={segment.key}
            style={{
              width: `${segment.percent ?? '0'}%`,
              backgroundColor: segment.colour,
            }}
          />
        ))}
      </div>

      <ul className="mt-3 space-y-2">
        {segments.map((segment) => (
          <li key={segment.key} className="flex items-center gap-2 text-note">
            <span
              className="size-2 shrink-0 rounded-full"
              style={{ backgroundColor: segment.colour }}
              aria-hidden="true"
            />
            <span className="text-dim">{segment.label}</span>
            <span className="ml-auto tabular-nums text-ink">
              {formatPercent(segment.percent)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
