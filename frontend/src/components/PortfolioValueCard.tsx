import type { Valuation } from '../api'
import { formatCurrency } from '../format'
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

/** The headline number and where it comes from. */
export function PortfolioValueCard({ valuation }: { valuation: Valuation }) {
  return (
    <Card className="flex flex-col min-[1100px]:min-h-[400px]">
      <p className="text-label text-faint">Portfolio Value</p>
      <p className="mt-1 tabular-nums text-figure font-semibold leading-tight text-ink">
        {formatCurrency(valuation.total_value)}
      </p>
      <p className="mt-1 text-note text-dim">Based on demo prices</p>

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
