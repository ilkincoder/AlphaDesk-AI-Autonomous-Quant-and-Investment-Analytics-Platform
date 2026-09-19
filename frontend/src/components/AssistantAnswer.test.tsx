/** One answer, rendered: the Markdown, the citations, the dates, and the dangers.
 *
 * The citation tests are the ones that matter. `E1` appears in every answer, so the only
 * question worth asking is whether the `E1` in *this* answer points at *this* answer's
 * evidence -- and whether a `javascript:` link in a filing passage can become a click.
 */

import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { aTurn } from '../testing'
import { AssistantAnswer } from './AssistantAnswer'

function withPassage() {
  return aTurn({
    answer: 'Export controls matter [E1]. A second point [E2].',
    evidence: [
      {
        reference: 'E1',
        kind: 'filing_passage',
        tool: 'filing_evidence_search',
        symbol: 'NVDA',
        label: 'filing passage 1 from 0001045810-26-000021',
        status: 'ok',
        summary: { quoted_text: 'Export controls restrict sales in several markets.' },
        citation: {
          accession_number: '0001045810-26-000021',
          form_type: '10-K',
          acceptance_datetime: '2026-02-25T21:42:19+00:00',
          report_date: '2026-01-25',
          source_url:
            'https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-20260125.htm',
          section: 'risk_factors',
          similarity: 0.7482627,
          document_name: 'nvda-20260125.htm',
          document_role: 'primary',
          content_sha256: 'a'.repeat(64),
        },
        trimmed: false,
      },
      {
        reference: 'E2',
        kind: 'tool_result',
        tool: 'portfolio_context',
        symbol: 'NVDA',
        label: 'portfolio_context result for NVDA',
        status: 'ok',
        summary: { price_source: 'demo' },
        citation: null,
        trimmed: false,
      },
    ],
    tool_executions: [
      {
        tool: 'filing_evidence_search',
        arguments: { question: 'export controls' },
        status: 'ok',
        reason: null,
        evidence_refs: ['E1'],
        rejection_code: null,
        warnings: [],
        reused_previous_result: false,
        rejected: null,
      },
      {
        tool: 'portfolio_context',
        arguments: {},
        status: 'ok',
        reason: null,
        evidence_refs: ['E2'],
        rejection_code: null,
        warnings: [],
        reused_previous_result: false,
        rejected: null,
      },
    ],
  })
}

describe('AssistantAnswer', () => {
  it('renders the answer’s Markdown, not its source', () => {
    render(
      <AssistantAnswer
        turn={aTurn({ answer: '## Head\n\n**bold** and `code`\n\n- one\n- two' })}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Head' })).toBeDefined()
    expect(screen.getByText('bold').tagName).toBe('STRONG')
    expect(screen.getByText('code').tagName).toBe('CODE')
    // By their text, because the limitations and the sources are lists too.
    expect(screen.getByText('one').tagName).toBe('LI')
    expect(screen.getByText('two').tagName).toBe('LI')
  })

  it('renders a table so it can scroll rather than widen the page', () => {
    render(
      <AssistantAnswer
        turn={aTurn({ answer: '| a | b |\n|---|---|\n| 1 | 2 |' })}
      />,
    )

    const table = screen.getByRole('table')
    // The table lives inside a scroller, which is what keeps a wide one from overflowing.
    expect(table.parentElement?.className).toContain('overflow-x-auto')
  })

  it('makes a citation a control when that reference is evidence for this answer', () => {
    render(<AssistantAnswer turn={withPassage()} />)

    expect(screen.getByRole('button', { name: 'Show evidence E1' })).toBeDefined()
  })

  it('leaves an unresolved citation as plain text, not a dead control', () => {
    render(<AssistantAnswer turn={aTurn({ answer: 'Something [E9].', evidence: [] })} />)

    expect(screen.queryByRole('button', { name: 'Show evidence E9' })).toBeNull()
    expect(screen.getByText(/\[E9\]/)).toBeDefined()
  })

  it('scopes evidence ids by turn, so E1 in two answers is two targets', () => {
    const first = withPassage()
    const second = withPassage()
    second.turn_id = 'turn-2'
    second.answer = 'A different answer [E1].'

    render(
      <>
        <AssistantAnswer turn={first} />
        <AssistantAnswer turn={second} />
      </>,
    )

    // Two controls, two distinct targets: clicking the second answer's E1 must not open the
    // first answer's evidence.
    const controls = screen.getAllByRole('button', { name: 'Show evidence E1' })
    expect(controls).toHaveLength(2)

    fireEvent.click(controls[1])
    const target = document.getElementById('evidence-turn-2-E1')
    expect(target).not.toBeNull()
    expect(document.getElementById('evidence-turn-1-E1')).not.toBeNull()
    // And they really are different elements.
    expect(target).not.toBe(document.getElementById('evidence-turn-1-E1'))
  })

  it('opens the sources when a citation is pressed', () => {
    render(<AssistantAnswer turn={withPassage()} />)

    const sources = screen.getByText(/Sources and evidence/).closest('details')
    expect(sources?.open).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Show evidence E1' }))

    expect(screen.getByText(/Sources and evidence/).closest('details')?.open).toBe(true)
  })

  it('shows what the evidence actually holds, including the filing it came from', () => {
    render(<AssistantAnswer turn={withPassage()} />)

    const sources = screen.getByText(/Sources and evidence/).closest('details') as HTMLElement
    const panel = within(sources)

    expect(panel.getAllByText(/0001045810-26-000021/).length).toBeGreaterThan(0)
    expect(panel.getByText('risk_factors')).toBeDefined()
    expect(panel.getByText('10-K')).toBeDefined()
    expect(panel.getByText(/Export controls restrict sales/)).toBeDefined()
    // A similarity, named as one, and never as a confidence.
    expect(panel.getByText(/not a probability/)).toBeDefined()
  })

  it('links to the filing using the URL the evidence carries', () => {
    render(<AssistantAnswer turn={withPassage()} />)

    const link = screen.getByRole('link', { name: /Open the filing/ })
    expect(link.getAttribute('href')).toBe(
      'https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-20260125.htm',
    )
  })

  it('does not render raw HTML from the answer', () => {
    render(
      <AssistantAnswer
        turn={aTurn({ answer: 'Before <script>window.hacked = true</script> after' })}
      />,
    )

    expect(document.querySelector('script')).toBeNull()
    expect((window as unknown as { hacked?: boolean }).hacked).toBeUndefined()
  })

  it('does not turn a dangerous URL into a click', () => {
    render(
      <AssistantAnswer
        turn={aTurn({ answer: '[click me](javascript:alert(1)) and [also](http://x.test)' })}
      />,
    )

    expect(screen.queryByRole('link', { name: 'click me' })).toBeNull()
    expect(screen.queryByRole('link', { name: 'also' })).toBeNull()
    // The words survive; only the link does not.
    expect(screen.getByText('click me')).toBeDefined()
  })

  it('keeps an https link clickable', () => {
    render(<AssistantAnswer turn={aTurn({ answer: '[filing](https://www.sec.gov/x)' })} />)

    expect(screen.getByRole('link', { name: 'filing' }).getAttribute('href')).toBe(
      'https://www.sec.gov/x',
    )
  })

  it('labels the comparison period and the information date separately', () => {
    render(<AssistantAnswer turn={aTurn()} />)

    expect(screen.getByText('Market comparison period')).toBeDefined()
    expect(screen.getByText('2026-08-06 → 2026-09-17')).toBeDefined()
    expect(screen.getByText('Overall information date')).toBeDefined()
    expect(screen.getByText('2026-09-19')).toBeDefined()
  })

  it('does not present a tool’s own cutoff as the run’s information date', () => {
    // A market tool reports 2026-09-18T04:00Z inside its payload, derived from the window's
    // end. The run's own date is the 19th, and only that one belongs in the summary.
    render(<AssistantAnswer turn={aTurn()} />)

    const information = screen.getByText('2026-09-19')
    expect(information).toBeDefined()
    expect(screen.queryByText('2026-09-18T04:00:00Z')).toBeNull()
  })

  it('says the portfolio values are demo values when the portfolio tool ran', () => {
    render(<AssistantAnswer turn={withPassage()} />)

    expect(screen.getByText(/not a live or historical valuation/)).toBeDefined()
  })

  it('does not claim demo values when no portfolio tool ran', () => {
    render(<AssistantAnswer turn={aTurn({ tool_executions: [] })} />)

    expect(screen.queryByText(/not a live or historical valuation/)).toBeNull()
  })

  it('shows the limitations the answer came with', () => {
    render(<AssistantAnswer turn={aTurn({ limitations: ['insufficient_coverage'] })} />)

    expect(screen.getByText('Limitations')).toBeDefined()
    expect(screen.getByText('insufficient_coverage')).toBeDefined()
  })

  it('shows a failure when there is no answer text', () => {
    render(
      <AssistantAnswer
        turn={aTurn({ answer: null, failure: 'The analysis could not be completed.' })}
      />,
    )

    expect(screen.getByText('The analysis could not be completed.')).toBeDefined()
  })

  it('keeps tool statuses out of the way but reachable', () => {
    render(<AssistantAnswer turn={aTurn()} />)

    const details = screen.getByText('Tool details and usage').closest('details')
    expect(details?.open).toBe(false)
    expect(within(details as HTMLElement).getByText(/market_insider_analysis/)).toBeDefined()
    expect(within(details as HTMLElement).getByText(/5 model requests/)).toBeDefined()
  })
})
