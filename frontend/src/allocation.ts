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
  /** The money this segment is, as the API's own string, when the caller has it to give.
   *
   * Optional because the two callers that draw a *proposed* allocation have percentages and no
   * money -- a target weight is not a value. A legend that has both shows both; one that has
   * only a share shows only that, which is why this is not a required field with a placeholder.
   */
  value?: string | null
}

/** One allocation as its segments, cash last.
 *
 * `cashValue` is the opt-in for money: pass the cash balance -- or `null`, when the balance is
 * unknown -- and every segment also carries the value it represents. **Omit the argument and the
 * segments carry shares alone, exactly as before**, which is what the projected allocations need:
 * a target weight produced by a proposal is a share of a portfolio, not an amount of money, and
 * a legend that showed a figure there would be labelling an estimate as a balance.
 */
export function allocationAssets(
  holdings: ValuationHolding[],
  cashAllocationPercent: string | null,
  cashValue?: string | null,
): AllocationAsset[] {
  const withValues = cashValue !== undefined
  return [
    ...holdings.map((holding, index) => ({
      key: holding.symbol,
      label: holding.symbol,
      percent: holding.allocation_percent,
      colour: HOLDING_SHADES[index % HOLDING_SHADES.length],
      value: withValues ? holding.holding_value : undefined,
    })),
    {
      key: 'CASH',
      label: 'Cash',
      percent: cashAllocationPercent,
      colour: CASH_SHADE,
      value: cashValue,
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
