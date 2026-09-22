import type { Valuation } from '../api'
import { formatCurrency, formatSyncTime } from '../format'
import { AllocationBar } from './AllocationBar'
import { Card } from './Card'

function Amount({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-label text-faint">{label}</p>
      <p className="mt-0.5 tabular-nums text-body text-ink">{formatCurrency(value)}</p>
    </div>
  )
}

/** Where the headline number came from.
 *
 * A demo price and a broker price are different kinds of fact, so the line under the
 * figure names which one it is. `price_source` decides, not a guess from the values.
 */
function basisNote(valuation: Valuation): string {
  if (valuation.price_source === 'demo') return 'Based on demo prices'
  const syncedAt = formatSyncTime(valuation.last_synced_at)
  if (syncedAt === null)
    return `Valuation supplied by ${valuation.price_source}; not synced yet`
  return `Based on ${valuation.price_source} prices, as of ${syncedAt}`
}

/** The headline number and where it comes from. */
export function PortfolioValueCard({ valuation }: { valuation: Valuation }) {
  return (
    <Card className="flex flex-col min-[1100px]:min-h-[400px]">
      <p className="text-label text-faint">Portfolio Value</p>
      <p className="mt-1 tabular-nums text-figure font-semibold leading-tight text-ink">
        {formatCurrency(valuation.total_value)}
      </p>
      <p className="mt-1 text-note text-dim">{basisNote(valuation)}</p>

      <div className="mt-5 grid grid-cols-2 gap-4">
        <Amount label="Holdings Value" value={valuation.holdings_value} />
        <Amount label="Cash Balance" value={valuation.cash_balance} />
      </div>

      <div className="mt-6 border-t border-line pt-5">
        <h3 className="text-heading font-medium text-ink">Allocation</h3>
        <div className="mt-3">
          <AllocationBar
            holdings={valuation.holdings}
            cashAllocationPercent={valuation.cash_allocation_percent}
          />
        </div>
      </div>
    </Card>
  )
}
