/** The proposal panel, driven by fixtures rather than by the API.
 *
 * The assertions that matter here are about what the panel *refuses* to say as much as what it
 * shows. It must not offer an approval or an execution it cannot perform, must not present a
 * model's prose as evidence, must not let a reader mistake a sell for a buy by colour alone, and
 * must not draw the before and after allocations as one chart. Each of those is a way a
 * financial panel usually goes wrong, and each is checked directly.
 */

import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { aProposal } from '../testing'
import { RebalanceProposal } from './RebalanceProposal'

function renderPanel(overrides = {}, onDismiss = vi.fn()) {
  render(<RebalanceProposal proposal={aProposal(overrides)} onDismiss={onDismiss} />)
  return onDismiss
}

describe('RebalanceProposal', () => {
  it('shows the proposals status and the snapshot it was computed from', () => {
    renderPanel()

    expect(screen.getByRole('heading', { name: 'Rebalance proposal' })).toBeTruthy()
    // Twice on purpose: once as the panel's status line, once as the summary card's recorded
    // status. They are the same sentence because they are the same fact, read from one function.
    expect(screen.getAllByText('Proposed · Estimates only')).toHaveLength(2)
    expect(screen.getByText(/^Portfolio snapshot:/)).toBeTruthy()
  })

  it('lists every proposed trade with the action as a word, not only a colour', () => {
    renderPanel()

    // Two tables on the panel now -- the trades and the target schedule -- so the trade table is
    // named by its caption rather than by being the only one.
    const table = screen.getByRole('table', { name: /Proposed whole-share orders/ })
    const rows = within(table).getAllByRole('row').slice(1)

    expect(rows).toHaveLength(2)
    // Both directions are present, and each says which it is in words. Red and green are the
    // least reliable way to say "down" and "up", so they are never the only way.
    expect(within(rows[0]).getByText('Sell')).toBeTruthy()
    expect(within(rows[0]).getByText('AAPL')).toBeTruthy()
    expect(within(rows[1]).getByText('Buy')).toBeTruthy()
    expect(within(rows[1]).getByText('MSFT')).toBeTruthy()
  })

  it('shows the values the calculation produced rather than recomputing them', () => {
    renderPanel()

    const table = screen.getByRole('table', { name: /Proposed whole-share orders/ })
    expect(within(table).getByText('$200.00')).toBeTruthy()
    expect(within(table).getByText('$1,000.00')).toBeTruthy()
    // The totals beside the table are the API's own strings.
    expect(screen.getByText('$1,100.00')).toBeTruthy()
  })

  it('draws the before and after allocations as two separate charts', () => {
    renderPanel()

    expect(screen.getByText('Before')).toBeTruthy()
    expect(screen.getByText('After')).toBeTruthy()

    // Scoped to the impact card, because AAPL and MSFT also appear in the trade table above.
    const impact = screen
      .getByRole('heading', { name: 'Impact on portfolio' })
      .closest('section') as HTMLElement
    // Each side is its own complete allocation: every holding *and* cash, once per ring.
    expect(within(impact).getAllByText('AAPL')).toHaveLength(2)
    expect(within(impact).getAllByText('MSFT')).toHaveLength(2)
    expect(within(impact).getAllByText('Cash')).toHaveLength(2)
    // The charts themselves are decorative; the legends are the readable version of them.
    expect(document.querySelectorAll('svg[aria-hidden="true"] circle').length).toBeGreaterThan(0)
  })

  it('labels the scenario as an assumption with its calculated impact', () => {
    renderPanel()

    expect(screen.getByText('Assumed scenario')).toBeTruthy()
    expect(screen.getByText(/AAPL at -10%, assumed rather than\s+forecast/)).toBeTruthy()
    expect(screen.getByText('-$1,000.00')).toBeTruthy()
  })

  it('keeps the rationale labelled as prose a model wrote', () => {
    renderPanel()

    const rationale = screen.getByRole('heading', { name: 'Rationale' })
    const card = rationale.closest('section') as HTMLElement

    expect(within(card).getByText(/points to a smaller position/)).toBeTruthy()
    expect(
      within(card).getByText(/Written by the proposal agent .* not a statement of fact/),
    ).toBeTruthy()
  })

  it('cites evidence with a link, a publisher and a publication date', () => {
    renderPanel()

    const link = screen.getByRole('link', { name: 'Analyst Sees More Upside for Microsoft' })

    expect(link.getAttribute('href')).toBe('https://www.benzinga.com/story/1')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    expect(screen.getByText(/benzinga · Sep 22/)).toBeTruthy()
  })

  it('states its assumptions and its limitations', () => {
    renderPanel()

    expect(
      screen.getByText('Existing long stock holdings plus cash only.'),
    ).toBeTruthy()
    expect(screen.getByText('Estimated only. Nothing was sent to a broker.')).toBeTruthy()
  })

  it('offers no approval, submission or trade control, and says why', () => {
    renderPanel()

    // The whole milestone ends at generation. A button that looked like it could execute would
    // be the single most misleading thing this panel could contain.
    for (const forbidden of ['Approve', 'Submit', 'Execute', 'Trade', 'Place order']) {
      expect(screen.queryByRole('button', { name: new RegExp(forbidden, 'i') })).toBeNull()
    }
    expect(screen.getByText(/No orders have been sent/)).toBeTruthy()
    expect(screen.getByText('Approval and execution are not available yet.')).toBeTruthy()
  })

  it('keeps run identifiers out of the summary', () => {
    renderPanel()

    expect(screen.queryByText(/thread|checkpoint/i)).toBeNull()
  })

  it('explains a no-change outcome rather than showing an empty table', () => {
    const base = aProposal()
    renderPanel({
      status: 'no_change',
      calculation: {
        ...base.calculation!,
        outcome: 'no_change',
        trades: [],
        sell_proceeds: '0.00',
        buy_cost: '0.00',
      },
    })

    expect(screen.getAllByText('No change recommended')).toHaveLength(2)
    // No trades to list, so no trade table -- but the target schedule still shows what was asked
    // for and why, which is the explanation the empty table is standing in for.
    expect(
      screen.queryByRole('table', { name: /Proposed whole-share orders/ }),
    ).toBeNull()
    expect(screen.getByText(/every order rounds to zero whole shares/)).toBeTruthy()
    expect(screen.getByRole('table', { name: /Target weights per holding/ })).toBeTruthy()
  })

  it('says a purchase depends on the sales funding it', () => {
    const base = aProposal()
    renderPanel({
      calculation: { ...base.calculation!, buys_depend_on_sells: true },
    })

    expect(screen.getByText(/depend on the sales\s+above executing first/)).toBeTruthy()
  })

  it('asks for a regeneration when the portfolio itself has changed', () => {
    renderPanel({
      freshness: 'portfolio_changed',
      freshness_reason:
        "The portfolio has changed since this proposal was generated, so it needs regenerating: AAPL's quantity moved from 50 to 60.",
    })

    expect(screen.getByText(/This proposal needs regenerating/)).toBeTruthy()
    expect(screen.getByText(/AAPL's quantity moved from 50 to 60/)).toBeTruthy()
  })

  it('keeps a proposal readable when only its prices have moved', () => {
    // A price that ticked is not a portfolio that changed, and discarding a correct answer over
    // one would be worse than showing it with the caveat.
    renderPanel({
      freshness: 'prices_updated',
      freshness_reason:
        'The portfolio holds the same positions in the same quantities, and its valuation ' +
        'prices have moved since this proposal was generated: MSFT from 450.00 to 500.00.',
    })

    expect(screen.getByText('Prices have moved since this proposal was generated.')).toBeTruthy()
    expect(screen.getByText(/same positions in the same quantities/)).toBeTruthy()
    // Still a proposal: the trades are there, and so is the schedule.
    expect(screen.getByRole('table', { name: /Proposed whole-share orders/ })).toBeTruthy()
    expect(screen.queryByText(/needs regenerating/)).toBeNull()
  })

  it('says freshness is unknown rather than calling an unreadable portfolio current', () => {
    renderPanel({
      freshness: 'unknown',
      freshness_reason:
        "The portfolio's holdings can no longer all be priced (missing: MSFT), so this " +
        'proposal cannot be checked against the portfolio as it stands now.',
    })

    expect(screen.getByText('Freshness unknown.')).toBeTruthy()
    expect(screen.getByText(/can no longer all be priced/)).toBeTruthy()
    expect(screen.queryByText(/needs regenerating/)).toBeNull()
  })

  it('says nothing when nothing has moved, however old the proposal is', () => {
    renderPanel({ freshness: 'current', freshness_reason: null })

    expect(screen.queryByText(/needs regenerating/)).toBeNull()
    expect(screen.queryByText(/Prices have moved/)).toBeNull()
    expect(screen.queryByText('Freshness unknown.')).toBeNull()
  })

  it('renders an unavailable proposal without inventing any trades', () => {
    renderPanel({
      status: 'unavailable',
      calculation: null,
      targets: null,
      rationale: null,
      failure: 'No article could be retrieved for this portfolio.',
      failure_reason: 'no_evidence',
    })

    expect(screen.getAllByText('Unavailable')).toHaveLength(2)
    expect(screen.getByText('No article could be retrieved for this portfolio.')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
    // Still said, whatever the outcome: nothing was sent anywhere.
    expect(screen.getByText(/No orders have been sent/)).toBeTruthy()
  })

  it('dismisses on request, and dismissing is all it does', () => {
    const onDismiss = renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))

    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})
