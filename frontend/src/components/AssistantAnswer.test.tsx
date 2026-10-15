/** One answer, rendered: the Markdown, the citations, the dates, and the dangers.
 *
 * The citation tests are the ones that matter. `E1` appears in every answer, so the only
 * question worth asking is whether the `E1` in *this* answer points at *this* answer's
 * evidence -- and whether a `javascript:` link in a filing passage can become a click.
 */

import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { aTurn } from '../testing'
import { AnswerDraft, AssistantAnswer } from './AssistantAnswer'

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

/** A tool result carrying what a real insider analysis carries: one row per transaction, each
 *  naming the Form 4 it was read from. The URLs are the SEC archive ones, not invented ones. */
function insiderResult(rows = 2) {
  return {
    transactions: {
      included: Array.from({ length: rows }, (_, index) => ({
        accession_number: `0001045810-26-00003${index + 1}`,
        source_document_url: `https://www.sec.gov/Archives/edgar/data/1045810/00010458102600003${index + 1}/form4.xml`,
      })),
    },
  }
}

/** A financial-facts result, whose source documents are 10-Ks -- `.htm`, so already readable. */
function financialFactsResult() {
  return {
    observations: [
      {
        accession_number: '0001045810-26-000021',
        concept: 'Revenues',
        source_reference:
          'https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-20260125.htm',
      },
    ],
  }
}

function withToolResult(summary: Record<string, unknown>) {
  return aTurn({
    evidence: [
      {
        reference: 'E1',
        kind: 'tool_result',
        tool: 'market_insider_analysis',
        symbol: 'NVDA',
        label: 'market_insider_analysis result for NVDA',
        status: 'ok',
        summary,
        citation: null,
        trimmed: false,
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

  it('strips the reference markers out of the answer, space and all', () => {
    render(<AssistantAnswer turn={withPassage()} />)

    // The sources are listed under the answer. A marker in the prose is not a second copy of
    // them, it is something in the middle of a sentence -- and the space in front of it goes
    // too, or the full stop would be left with a gap before it.
    expect(screen.queryByText(/\[E\d+\]/)).toBeNull()
    expect(screen.getByText('Export controls matter. A second point.')).toBeDefined()
  })

  it('strips a marker even when it names nothing this turn has', () => {
    render(<AssistantAnswer turn={aTurn({ answer: 'Something [E9].', evidence: [] })} />)

    // Stripping is by shape, not by whether the reference resolves: what the reader must not
    // see is the marker itself, and a stray one is exactly the case where it would show.
    expect(screen.getByText('Something.')).toBeDefined()
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

  it('links to the filings a structured result came from', () => {
    render(<AssistantAnswer turn={withToolResult(insiderResult())} />)

    const link = screen.getByRole('link', { name: /0001045810-26-000031/ })
    // The readable page for the filing, not the Form 4's XML: a browser renders that as a raw
    // tree, which is not something a reader can use.
    expect(link.getAttribute('href')).toBe(
      'https://www.sec.gov/Archives/edgar/data/1045810/000104581026000031/0001045810-26-000031-index.htm',
    )
  })

  it('links a document that already opens as a page unchanged', () => {
    render(<AssistantAnswer turn={withToolResult(financialFactsResult())} />)

    expect(
      screen.getByRole('link', { name: /0001045810-26-000021/ }).getAttribute('href'),
    ).toBe(
      'https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-20260125.htm',
    )
  })

  it('lists one link per filing, however many rows cite it', () => {
    const summary = insiderResult()
    const transactions = (summary.transactions as { included: unknown[] }).included
    // Two rows on the same form, which is the ordinary case: one Form 4 can report
    // several transactions.
    transactions.push(transactions[0])

    render(<AssistantAnswer turn={withToolResult(summary)} />)

    expect(screen.getAllByRole('link', { name: /0001045810-26-000031/ })).toHaveLength(1)
  })

  it('counts the filings it does not list, so a short list is not read as the whole one', () => {
    render(<AssistantAnswer turn={withToolResult(insiderResult(7))} />)

    expect(screen.getAllByRole('link', { name: /Open filing/ })).toHaveLength(5)
    expect(screen.getByText('and 2 more')).toBeDefined()
  })

  it('does not turn a source URL that is not https into a click', () => {
    const summary = insiderResult()
    const transactions = summary.transactions as {
      included: { source_document_url: string }[]
    }
    transactions.included[0].source_document_url = 'javascript:alert(1)'

    render(<AssistantAnswer turn={withToolResult(summary)} />)

    expect(screen.queryByRole('link', { name: /0001045810-26-000031/ })).toBeNull()
  })

  it('rewrites only documents that are really SEC archive documents', () => {
    const summary = insiderResult()
    const transactions = summary.transactions as {
      included: { source_document_url: string }[]
    }
    // An XML document somewhere else is not assumed to have an EDGAR filing page beside it.
    transactions.included[0].source_document_url = 'https://example.test/data/form4.xml'

    render(<AssistantAnswer turn={withToolResult(summary)} />)

    expect(
      screen.getByRole('link', { name: /0001045810-26-000031/ }).getAttribute('href'),
    ).toBe('https://example.test/data/form4.xml')
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

describe('AnswerDraft', () => {
  it('shows the prose and says plainly that nothing has checked it', () => {
    render(<AnswerDraft text="NVDA rose slightly over the window." />)

    expect(screen.getByText('Answer being written')).toBeDefined()
    expect(screen.getByText('Not checked yet')).toBeDefined()
    expect(screen.getByText(/sources have not been checked yet/)).toBeDefined()
    expect(screen.getByText('NVDA rose slightly over the window.')).toBeDefined()
  })

  it('strips a reference from the draft as well as from the answer', () => {
    render(<AnswerDraft text="Insiders were sellers [E1]." />)

    // The draft and the answer are the same kind of text, and a marker must not survive in
    // either -- including here, where the run that wrote it can still withdraw the sentence.
    expect(screen.getByText('Insiders were sellers.')).toBeDefined()
  })

  it('does not make a link clickable in text that is still being written', () => {
    render(<AnswerDraft text="See [the filing](https://www.sec.gov/Archives/x.htm)." />)

    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText('the filing')).toBeDefined()
  })
})
