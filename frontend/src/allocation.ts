/** The colours an allocation is drawn in, decided once for the whole application.
 *
 * Two components now draw the same portfolio — the allocation bar on the Portfolio and What
 * if pages, and the before/after donuts in a rebalance proposal — and the point of a portfolio
 * chart is that the same holding is the same colour everywhere. A palette copied into the
 * second component is a palette that drifts from the first.
 *
 * **Colour is by position, not by symbol.** A symbol hashed to a colour would need as many
 * shades as there are tickers; cycling five shades by position is what the existing bar does,
 * and both sides of a proposal arrive in the same order — `calculate_valuation` sorts by symbol
 * on both — so the same holding gets the same colour on both donuts without a symbol map.
 *
 * Nothing here parses a percentage. Every value is the string the API sent, used as given:
 * Day 3 rounds each percentage independently and says so, which is why a set of segments need
 * not add to exactly 100.
 */

import type { ValuationHolding } from './api'

/** Purple shades for holdings, in API order. Cycled, so a portfolio with more holdings than
 *  shades repeats rather than running out and rendering nothing. */
export const HOLDING_SHADES = ['#8B5CF6', '#A78BFA', '#7C3AED', '#6D28D9', '#C4B5FD']

/** Cash is deliberately not purple: it is not a holding. */
export const CASH_SHADE = 'var(--color-cash)'

export type AllocationAsset = {
  /** Stable across renders and unique within one allocation. */
  key: string
  label: string
  /** The API's own string, or null when the total is zero and a share is undefined. */
  percent: string | null
  colour: string
}

/** One allocation as its segments, cash last. */
export function allocationAssets(
  holdings: ValuationHolding[],
  cashAllocationPercent: string | null,
): AllocationAsset[] {
  return [
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
}

/** True when there is no denominator to take a share of.
 *
 * A zero-value portfolio has no allocation, and drawing one with every segment at zero would
 * look like a portfolio that has lost everything rather than one worth nothing.
 */
export function nothingToAllocate(assets: AllocationAsset[]): boolean {
  return assets.every((asset) => asset.percent === null)
}
