/** Allocation, as one bar plus a legend.
 *
 * Every percentage shown here comes from the API. The browser multiplies nothing: a
 * segment's width is the string the backend sent, used as a CSS percentage, and a
 * legend entry is that same string printed. That is why the segments need not add to
 * exactly 100 — Day 3 rounds each percentage independently and says so.
 */

import { allocationAssets, nothingToAllocate } from '../allocation'
import type { ValuationHolding } from '../api'
import { formatPercent } from '../format'

export function AllocationBar({
  holdings,
  cashAllocationPercent,
}: {
  holdings: ValuationHolding[]
  cashAllocationPercent: string | null
}) {
  // The palette and the segment order come from `allocation.ts`, so this bar and the
  // proposal's donuts cannot disagree about what colour a holding is.
  const segments = allocationAssets(holdings, cashAllocationPercent)

  // A zero-value portfolio has no denominator, so the API sends null for every
  // percentage. Drawing an empty bar with no explanation would look like a bug.
  if (nothingToAllocate(segments)) {
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
