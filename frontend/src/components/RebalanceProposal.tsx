/** The rebalance proposal, inline below the news.
 *
 * **Every number here was calculated in Python.** The trade quantities, the reference prices,
 * the estimated values, the two allocations and the scenario impact all come out of
 * `app/rebalance.py` and are read straight from the response. Nothing on this page multiplies a
 * quantity by a price, and nothing rounds one: the only arithmetic below is choosing which
 * string to show. That is the whole reason the deterministic calculation exists as a separate
 * step from the agent that proposed the targets, and it would be undone by a component that
 * "helpfully" recomputed a total.
 *
 * **Nothing here is an order.** There is no approve button, no submit button and no
 * broker call anywhere in this milestone, and the footer says so in as many words. The
 * proposal is a record of what this application calculated, and the page must not let it read
 * as anything more.
 *
 * **The rationale is prose a model wrote.** It is labelled as such and kept apart from the
 * evidence: every citation under it resolves to a stored article whose publisher and
 * publication date are shown, and the API refuses a rationale citing one that does not.
 */

import { allocationAssets } from '../allocation'
import type { RebalanceProposal as Proposal } from '../api'
import {
  formatCurrency,
  formatDateTime,
  formatPercent,
  formatShares,
  formatSignedPercent,
} from '../format'
import { AllocationDonut } from './AllocationDonut'
import { Button } from './Button'
import { Card } from './Card'
import { Banner } from './States'

/** How a status reads to a person. One place, so the heading and the summary card cannot
 *  describe the same proposal differently.
 *
 *  Deliberately absent: approved, submitted, filled. There is no status here this milestone
 *  can reach, and a label for one would be a promise the application does not keep. */
function statusLabel(status: string): string {
  switch (status) {
    case 'proposed':
      return 'Proposed · Estimates only'
    case 'no_change':
      return 'No change recommended'
    case 'generating':
      return 'Generating…'
    default:
      // `unavailable` and `interrupted` are both "there is no proposal here", and the sentence
      // beside this label is what distinguishes them.
      return 'Unavailable'
  }
}

function statusColour(status: string): string {
  if (status === 'proposed') return 'text-positive'
  if (status === 'no_change') return 'text-accent-ink'
  return 'text-dim'
}

export function RebalanceProposal({
  proposal,
  onDismiss,
}: {
  proposal: Proposal
  onDismiss: () => void
}) {
  const snapshot = proposal.snapshot
  const calculation = proposal.calculation

  return (
    <section aria-labelledby="rebalance-heading" className="mt-6 border-t border-line pt-6">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="rebalance-heading" className="text-heading font-semibold text-ink">
          Rebalance proposal
        </h2>
        {snapshot !== null && (
          <p className="text-label text-faint">
            Portfolio snapshot: {formatDateTime(snapshot.read_at) ?? 'unknown'}
          </p>
        )}
      </header>

      <p className={`mt-1 text-note font-medium ${statusColour(proposal.status)}`}>
        {statusLabel(proposal.status)}
      </p>

      <FreshnessNotice proposal={proposal} />

      {proposal.failure !== null && (
        <div className="mt-4">
          <Banner>
            <span>{proposal.failure}</span>
          </Banner>
        </div>
      )}

      {/* All three cards are drawn whatever the outcome. An unavailable proposal is exactly
          when a reader most needs to see that nothing was executed and what the limitations
          were, so those do not disappear along with the calculation. */}
      <div className="mt-4 grid grid-cols-1 gap-4 min-[1100px]:grid-cols-[36fr_34fr_30fr]">
        <TradesCard calculation={calculation} />
        <ImpactCard calculation={calculation} snapshot={snapshot} />
        <SummaryCard proposal={proposal} />
      </div>

      <div className="mt-4">
        <TargetsCard proposal={proposal} />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 min-[1100px]:grid-cols-3">
        <RationaleColumn proposal={proposal} />
        <EvidenceColumn proposal={proposal} />
        <AssumptionsColumn proposal={proposal} />
      </div>

      <footer className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <p className="text-note text-dim">
          No orders have been sent. Approval and execution are not available yet.
        </p>
        <Button onClick={onDismiss}>Dismiss</Button>
      </footer>
    </section>
  )
}

/** How the portfolio has moved since the proposal was generated, if it has.
 *
 * Four states, and they are not degrees of the same thing. A price that ticked leaves a proposal
 * that is still a faithful estimate of what was proposed against what was held -- throwing it
 * away would be discarding a correct answer. A quantity that moved means the arithmetic was
 * computed against a portfolio that no longer exists, which is a different claim and needs a
 * different response. And "it could not be checked" is neither of those: an outage must not be
 * reported as a change.
 */
function FreshnessNotice({ proposal }: { proposal: Proposal }) {
  if (proposal.freshness === 'current' || proposal.freshness_reason === null) return null

  if (proposal.freshness === 'prices_updated') {
    return (
      <div className="mt-4">
        <Card>
          <p className="text-note text-ink">Prices have moved since this proposal was generated.</p>
          <p className="mt-1 text-note text-dim">{proposal.freshness_reason}</p>
        </Card>
      </div>
    )
  }

  if (proposal.freshness === 'unknown') {
    return (
      <div className="mt-4">
        <Card>
          <p className="text-note text-ink">Freshness unknown.</p>
          <p className="mt-1 text-note text-dim">{proposal.freshness_reason}</p>
        </Card>
      </div>
    )
  }

  return (
    <div className="mt-4">
      <Banner>
        <span>
          This proposal needs regenerating. {proposal.freshness_reason} Generate a new one to
          work from the portfolio as it is now.
        </span>
      </Banner>
    </div>
  )
}

/** The target schedule: every holding and the cash, now, asked for, and after rounding.
 *
 * The reason is per line and it is the agent's own, carried through rather than composed here.
 * The citations are what tell the two kinds of reasoning apart: a line citing an article is
 * reasoned from the news, and a line citing nothing is reasoned from the policy or from the
 * portfolio's own shape.
 */
function TargetsCard({ proposal }: { proposal: Proposal }) {
  const lines = proposal.calculation?.allocations ?? []

  return (
    <Card>
      <h3 className="text-heading font-medium text-ink">Target allocation</h3>
      <p className="mt-1 text-label text-faint">
        Where each holding is now, what the agent asked for, and where whole-share rounding
        actually leaves it. The last two differ whenever an order cannot be an exact number of
        shares.
      </p>

      {lines.length === 0 ? (
        <p className="mt-3 text-body text-dim">
          No target weights were calculated, because no proposal was produced for this portfolio.
        </p>
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[520px] text-label">
            <caption className="sr-only">
              Target weights per holding and for cash, before and after rounding
            </caption>
            <thead>
              <tr className="text-left text-faint">
                <th scope="col" className="pb-2 pr-3 font-normal">Holding</th>
                <th scope="col" className="pb-2 pr-3 text-right font-normal">Now</th>
                <th scope="col" className="pb-2 pr-3 text-right font-normal">Target</th>
                <th scope="col" className="pb-2 pr-3 text-right font-normal">After rounding</th>
                <th scope="col" className="pb-2 font-normal">Why</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <tr key={line.symbol} className="border-t border-line align-top">
                  <td className="py-2 pr-3 text-ink">{line.symbol}</td>
                  <td className="py-2 pr-3 text-right tabular-nums text-dim">
                    {formatPercent(line.current_percent)}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums text-ink">
                    {formatPercent(line.target_percent)}
                    {/* The direction as a word, for the same reason the trade table has one. */}
                    <span className="ml-1 text-faint">
                      {line.movement === 'increase'
                        ? '▲'
                        : line.movement === 'decrease'
                          ? '▼'
                          : '='}
                    </span>
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums text-dim">
                    {formatPercent(line.achieved_percent)}
                  </td>
                  <td className="py-2 text-dim">
                    {line.reason === '' ? (
                      <span className="text-faint">No reason was given for this line.</span>
                    ) : (
                      <>
                        {line.reason}
                        <span className="mt-1 block text-faint">
                          {line.evidence_refs.length > 0
                            ? `From ${line.evidence_refs.join(', ')}`
                            : 'Policy and portfolio shape, not from an article'}
                        </span>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-3 text-label text-faint">
        Each weight is a discretionary choice made under this policy from the evidence retrieved
        for this run. None of them is optimal, derived from an expected return, or predicted to
        be profitable.
      </p>
    </Card>
  )
}

/** Card 1: every proposed trade, or why there are none. */
function TradesCard({ calculation }: { calculation: Proposal['calculation'] }) {
  return (
    <Card>
      <h3 className="text-heading font-medium text-ink">Proposed trades</h3>

      {calculation === null ? (
        <p className="mt-3 text-body text-dim">
          No trades were calculated, because no proposal could be produced for this portfolio.
          The reason is stated above, and nothing was sent to a broker.
        </p>
      ) : calculation.trades.length === 0 ? (
        <div className="mt-3">
          <p className="text-body text-ink">
            No trades are proposed. The target weights the agent suggested are close enough to
            what the portfolio already holds that every order rounds to zero whole shares.
          </p>
          <p className="mt-2 text-note text-dim">
            Holding everything is a result, not a failure to produce one. The target weights are
            under Rationale.
          </p>
        </div>
      ) : (
        <>
          {/* The table scrolls inside its own card rather than widening the page. */}
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[320px] text-label">
              <caption className="sr-only">
                Proposed whole-share orders and their estimated values
              </caption>
              <thead>
                <tr className="text-left text-faint">
                  <th scope="col" className="pb-2 pr-3 font-normal">
                    Symbol
                  </th>
                  <th scope="col" className="pb-2 pr-3 font-normal">
                    Action
                  </th>
                  <th scope="col" className="pb-2 pr-3 text-right font-normal">
                    Quantity
                  </th>
                  <th scope="col" className="pb-2 pr-3 text-right font-normal">
                    Ref. price
                  </th>
                  <th scope="col" className="pb-2 text-right font-normal">
                    Est. value
                  </th>
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {calculation.trades.map((trade) => (
                  <tr key={`${trade.symbol}-${trade.action}`} className="border-t border-line">
                    <td className="py-2 pr-3 text-ink">{trade.symbol}</td>
                    <td className="py-2 pr-3">
                      {/* The word carries the direction. Red and green are the least reliable
                          way to say "sell" and "buy", so they are never the only way. */}
                      <span
                        className={
                          trade.action === 'sell' ? 'text-danger' : 'text-positive'
                        }
                      >
                        {trade.action === 'sell' ? 'Sell' : 'Buy'}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-right text-ink">
                      {formatShares(trade.quantity)}
                    </td>
                    <td className="py-2 pr-3 text-right text-dim">
                      {formatCurrency(trade.reference_price)}
                    </td>
                    <td className="py-2 text-right text-ink">
                      {formatCurrency(trade.estimated_value)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <dl className="mt-3 space-y-1 text-label">
            <Line label="Estimated sales" value={formatCurrency(calculation.sell_proceeds)} />
            <Line label="Estimated purchases" value={formatCurrency(calculation.buy_cost)} />
            <Line label="Cash afterwards" value={formatCurrency(calculation.cash_after)} />
          </dl>

          {calculation.buys_depend_on_sells && (
            <p className="mt-3 text-label text-faint">
              The purchases cost more than the cash in the account, so they depend on the sales
              above executing first. Those proceeds are not available yet.
            </p>
          )}

          <p className="mt-3 text-label text-faint">
            Priced at the same prices the portfolio is valued at. These are estimates, not
            quotes.
          </p>
        </>
      )}
    </Card>
  )
}

/** Card 2: the two allocations, and the assumed scenario's impact. */
function ImpactCard({
  calculation,
  snapshot,
}: {
  calculation: Proposal['calculation']
  snapshot: Proposal['snapshot']
}) {
  // Both sides arrive from the same calculation, so a holding is the same colour on both rings.
  const before =
    calculation === null
      ? []
      : allocationAssets(
          calculation.before.holdings,
          calculation.before.cash_allocation_percent,
        )
  const after =
    calculation === null
      ? []
      : allocationAssets(
          calculation.after.holdings,
          calculation.after.cash_allocation_percent,
        )

  if (calculation === null) {
    return (
      <Card>
        <h3 className="text-heading font-medium text-ink">Impact on portfolio</h3>
        <p className="mt-3 text-body text-dim">
          There is no before-and-after to show, because no trades were calculated. The
          portfolio is unchanged and nothing was sent to a broker.
        </p>
      </Card>
    )
  }

  return (
    <Card>
      <h3 className="text-heading font-medium text-ink">Impact on portfolio</h3>
      <p className="mt-1 text-label text-faint">
        Share of the total, before and after the proposed trades.
      </p>

      {/* Two rings, two allocations. They are never added together: the current side is a share
          of the broker's equity and the proposed side is computed from holdings and cash, so
          the two need not even share a denominator. */}
      <div className="mt-3 grid grid-cols-2 gap-4">
        <AllocationDonut
          label="Before"
          assets={before}
          caption={`Total ${formatCurrency(calculation.before.total_value)}`}
        />
        <AllocationDonut
          label="After"
          assets={after}
          caption={`Total ${formatCurrency(calculation.after.total_value)}`}
        />
      </div>

      <div className="mt-4 border-t border-line pt-3">
        <h4 className="text-label font-medium text-ink">Assumed scenario</h4>
        <p className="mt-1 text-label text-faint">
          {calculation.scenario.symbol} at{' '}
          {formatSignedPercent(calculation.scenario.price_change_percent)}, assumed rather than
          forecast.
        </p>
        <dl className="mt-2 space-y-1 text-label">
          <Line
            label="Value before"
            value={formatCurrency(calculation.scenario.total_value_before)}
          />
          <Line
            label="Value after"
            value={formatCurrency(calculation.scenario.total_value_after)}
          />
          <Line
            label="Portfolio change"
            value={formatCurrency(calculation.scenario.change_value)}
          />
          <Line
            label="Change, as a share"
            value={formatSignedPercent(calculation.scenario.change_percent ?? '0')}
          />
        </dl>
        <p className="mt-2 text-label text-faint">
          Largest position afterwards:{' '}
          {calculation.largest_after.symbol}{' '}
          {formatPercent(calculation.largest_after.allocation_percent)}.
        </p>
      </div>

      {snapshot !== null && snapshot.reported_total_value !== null && (
        <p className="mt-3 text-label text-faint">
          The current share is of the broker's own equity figure
          {calculation.reconciliation.difference !== null
            ? ` (${formatCurrency(calculation.reconciliation.difference)} away from the summed holdings and cash)`
            : ''}
          . The proposed share is computed from the holdings and the cash.
        </p>
      )}
    </Card>
  )
}

/** Card 3: what this is, how fresh it is, and what it does not claim.
 *
 * Deliberately no run identifiers of any kind. The thread a run belongs to is an implementation
 * detail of the orchestration, and a card a reader is meant to act on is the wrong place for
 * one.
 */
function SummaryCard({ proposal }: { proposal: Proposal }) {
  const snapshot = proposal.snapshot
  const usage = proposal.usage ?? {}

  return (
    <Card>
      <h3 className="text-heading font-medium text-ink">Proposal summary</h3>
      <dl className="mt-3 space-y-1 text-label">
        <Line label="Status" value={statusLabel(proposal.status)} />
        <Line label="Generated" value={formatDateTime(proposal.created_at) ?? 'unknown'} />
        {snapshot !== null && (
          <>
            <Line
              label="Prices from"
              value={snapshot.price_source === 'demo' ? 'Demo price table' : snapshot.price_source}
            />
            <Line
              label="Last synchronised"
              value={formatDateTime(snapshot.last_synced_at) ?? 'never'}
            />
          </>
        )}
        {proposal.policy !== null && (
          <Line label="Policy" value={proposal.policy.name} />
        )}
        {typeof usage.model_requests === 'number' && (
          <Line label="Model requests" value={String(usage.model_requests)} />
        )}
      </dl>

      <p className="mt-3 text-label font-medium text-ink">
        Approval and execution are not available yet.
      </p>
      <p className="mt-1 text-label text-faint">
        Generating a proposal is not an approval and commits nothing. Nothing was sent to a
        broker, no order was placed, and no fill is modelled. Executing anything later would
        need fresh validation of the portfolio and an explicit decision.
      </p>

      {proposal.limitations.length > 0 && (
        <>
          <h4 className="mt-4 text-label font-medium text-ink">Limitations</h4>
          <ul className="mt-1 space-y-1.5 text-label text-faint">
            {proposal.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </>
      )}
    </Card>
  )
}

function RationaleColumn({ proposal }: { proposal: Proposal }) {
  return (
    <Card>
      <h3 className="text-heading font-medium text-ink">Rationale</h3>
      {proposal.rationale === null ? (
        <p className="mt-2 text-note text-dim">
          No rationale was recorded, because no proposal was produced.
        </p>
      ) : (
        <>
          <p className="mt-2 whitespace-pre-line text-body text-dim">{proposal.rationale}</p>
          {/* Said plainly, because the paragraph above reads like an argument and is not
              evidence: it is a model's reading of the articles listed beside it. */}
          <p className="mt-3 text-label text-faint">
            Written by the proposal agent from the evidence listed here. It is not a statement
            of fact and nothing on this page corroborates it.
          </p>
        </>
      )}

      <p className="mt-3 text-label text-faint">
        The numbers behind each sentence are in Target allocation above.
      </p>
    </Card>
  )
}

function EvidenceColumn({ proposal }: { proposal: Proposal }) {
  return (
    <Card>
      <h3 className="text-heading font-medium text-ink">Evidence</h3>
      {proposal.evidence.length === 0 ? (
        <p className="mt-2 text-note text-dim">
          No article was retrieved for this run, so there is nothing to cite.
        </p>
      ) : (
        <ul className="mt-2 space-y-3">
          {proposal.evidence.map((item) => {
            const published = formatDateTime(item.published_at)
            return (
              <li key={item.reference}>
                <h4 className="text-note font-medium text-ink">
                  {/* External, and marked as such: a publisher's article is not something this
                      application vouches for. */}
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="hover:text-accent-ink"
                  >
                    {item.title}
                  </a>
                </h4>
                <p className="mt-0.5 text-label text-faint">
                  {item.publisher}
                  {published !== null && ` · ${published}`}
                  {!item.recent && ' · older context'}
                </p>
              </li>
            )
          })}
        </ul>
      )}
    </Card>
  )
}

function AssumptionsColumn({ proposal }: { proposal: Proposal }) {
  return (
    <Card>
      <h3 className="text-heading font-medium text-ink">Assumptions</h3>
      {proposal.assumptions.length === 0 ? (
        <p className="mt-2 text-note text-dim">
          None were recorded, because the run stopped before it reached the point of proposing
          anything under this policy.
        </p>
      ) : (
        <ul className="mt-2 space-y-1.5 text-label text-dim">
          {proposal.assumptions.map((assumption) => (
            <li key={assumption}>{assumption}</li>
          ))}
        </ul>
      )}
    </Card>
  )
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-faint">{label}</dt>
      <dd className="text-right tabular-nums text-ink">{value}</dd>
    </div>
  )
}

