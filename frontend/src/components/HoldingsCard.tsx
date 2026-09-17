import type { Valuation } from '../api'
import { PLACEHOLDER, formatCurrency, formatPercent, formatShares } from '../format'
import { Card } from './Card'

const HEAD_CELL = 'px-3 py-2 text-left text-label font-normal text-faint'
const HEAD_CELL_NUMERIC = `${HEAD_CELL} text-right`
const NUMERIC_CELL = 'px-3 py-2 text-right tabular-nums'

/** The holdings table, with cash appended as a row rather than hidden in a footnote.
 *
 * Cash is not a holding, so it sits after the holdings and its Shares and Demo Price
 * cells are empty — a dash, not a zero, because there are no shares of cash.
 */
export function HoldingsCard({ valuation }: { valuation: Valuation }) {
  // The API already sorts by symbol; sorting again costs one line and means this
  // component does not silently depend on that promise staying true.
  const holdings = [...valuation.holdings].sort((a, b) => a.symbol.localeCompare(b.symbol))

  return (
    <Card className="flex flex-col min-[1100px]:min-h-[400px]">
      <h3 className="text-heading font-medium text-ink">Holdings</h3>

      {/* Scrolls on its own so a narrow screen never makes the whole page scroll
          sideways. */}
      <div className="mt-3 -mx-5 overflow-x-auto px-5">
        <table className="w-full min-w-[520px] border-collapse">
          <caption className="sr-only">
            Holdings valued at demo prices, with cash shown last
          </caption>
          <thead>
            <tr className="border-b border-line">
              <th scope="col" className={HEAD_CELL}>
                Symbol
              </th>
              <th scope="col" className={HEAD_CELL_NUMERIC}>
                Shares
              </th>
              <th scope="col" className={HEAD_CELL_NUMERIC}>
                Demo Price
              </th>
              <th scope="col" className={HEAD_CELL_NUMERIC}>
                Holding Value
              </th>
              <th scope="col" className={HEAD_CELL_NUMERIC}>
                Allocation
              </th>
            </tr>
          </thead>
          <tbody>
            {holdings.map((holding) => (
              <tr
                key={holding.symbol}
                className="h-12 border-b border-line transition-colors hover:bg-line/40"
              >
                <th scope="row" className="px-3 py-2 text-left font-medium text-ink">
                  {holding.symbol}
                </th>
                <td className={`${NUMERIC_CELL} text-dim`}>
                  {formatShares(holding.quantity)}
                </td>
                <td className={`${NUMERIC_CELL} text-dim`}>
                  {formatCurrency(holding.price)}
                </td>
                <td className={`${NUMERIC_CELL} text-ink`}>
                  {formatCurrency(holding.holding_value)}
                </td>
                <td className={`${NUMERIC_CELL} text-dim`}>
                  {formatPercent(holding.allocation_percent)}
                </td>
              </tr>
            ))}

            {holdings.length === 0 && (
              <tr className="h-12 border-b border-line">
                <td colSpan={5} className="px-3 py-2 text-dim">
                  No stock holdings yet
                </td>
              </tr>
            )}

            <tr className="h-12">
              <th scope="row" className="px-3 py-2 text-left font-medium text-ink">
                CASH
              </th>
              <td className={`${NUMERIC_CELL} text-faint`}>{PLACEHOLDER}</td>
              <td className={`${NUMERIC_CELL} text-faint`}>{PLACEHOLDER}</td>
              <td className={`${NUMERIC_CELL} text-ink`}>
                {formatCurrency(valuation.cash_balance)}
              </td>
              <td className={`${NUMERIC_CELL} text-dim`}>
                {formatPercent(valuation.cash_allocation_percent)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="mt-auto flex flex-wrap gap-x-8 gap-y-4 border-t border-line pt-5">
        <Summary label="Holdings Value" value={valuation.holdings_value} />
        <Summary label="Cash Balance" value={valuation.cash_balance} />
        <Summary label="Total Value" value={valuation.total_value} />
      </div>
    </Card>
  )
}

function Summary({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-label text-faint">{label}</p>
      <p className="mt-0.5 tabular-nums text-body text-ink">{formatCurrency(value)}</p>
    </div>
  )
}
