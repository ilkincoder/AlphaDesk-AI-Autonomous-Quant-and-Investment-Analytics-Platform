/** One assistant turn: the answer, what it rests on, and what it cannot say.
 *
 * The answer is Markdown because that is what the Supervisor writes -- headings, bold, lists
 * and tables, which is the shape a financial explanation actually takes. `react-markdown`
 * renders it with raw HTML **off**: the text is model output, and model output is not a
 * document anybody vetted. Links are restricted to `https:` and in-page anchors, so a
 * `javascript:` URL in a passage cannot become a click.
 *
 * Citations are the part worth reading closely. `[E1]` in the prose becomes a control only
 * when `E1` is in *this turn's* evidence, and its target id carries the turn: `E1` in two
 * different answers is two different pieces of evidence, and a bare `#E1` would have the second
 * answer's link open the first answer's source.
 */

import { useCallback, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import type { Evidence, FilingCitation, Turn } from '../analysisApi'
import { Card } from './Card'

/** Links the answer may contain. Anything else renders as plain text.
 *
 * `#` is allowed because citations are in-page anchors this component owns. `https:` is
 * allowed because SEC source links are https. `http:`, `javascript:`, `data:` and the rest are
 * not: a filing passage is untrusted text, and one of them turning into a live link is exactly
 * the thing this prevents.
 */
function safeHref(url: string | undefined): string | undefined {
  if (!url) return undefined
  if (url.startsWith('#evidence-')) return url
  if (url.startsWith('https://')) return url
  return undefined
}

function evidenceId(turnId: string, reference: string): string {
  // Prefixed with the turn so two answers citing `E1` cannot collide, and with `evidence-`
  // so a citation link and its target are recognisable as a pair.
  return `evidence-${turnId}-${reference}`
}

/** Turn `[E1]` into a link, but only where `E1` is evidence this turn actually has.
 *
 * An unresolved reference stays exactly as written -- plain text, not a control -- because the
 * backend deliberately withholds an answer whose citations do not resolve, and the one thing
 * worse than a dead link is a live one that opens something unrelated.
 */
function withCitationLinks(markdown: string, available: Set<string>, turnId: string): string {
  return markdown.replace(/\[(E\d+)\]/g, (whole, reference: string) =>
    available.has(reference) ? `[${reference}](#${evidenceId(turnId, reference)})` : whole,
  )
}

function Citation({ reference, onOpen }: { reference: string; onOpen: (ref: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onOpen(reference)}
      // The accessible name says what pressing it does; "E1" alone does not.
      aria-label={`Show evidence ${reference}`}
      className="mx-0.5 rounded border border-accent/40 bg-selected px-1 text-label text-accent-ink transition-colors hover:border-accent"
    >
      {reference}
    </button>
  )
}

const DATE_LABELS = {
  start_date: 'Market comparison period',
  information_date: 'Overall information date',
} as const

/** Company, the period compared, and the run's own information date.
 *
 * Labelled separately and deliberately. The market comparison period is what the price
 * comparison covers; the information date is what was knowable for the run as a whole, and it
 * is routinely later. A tool may report a narrower cutoff of its own, which belongs with that
 * tool's evidence -- shown in the sources below -- and never here, where it would read as the
 * run's.
 */
function ContextSummary({ turn }: { turn: Turn }) {
  const resolved = turn.resolved
  const symbol = resolved?.symbol ?? turn.symbol
  const window =
    resolved?.start_date && resolved?.end_date
      ? `${resolved.start_date} → ${resolved.end_date}`
      : null
  const informationDate = resolved?.as_of ?? null

  const isDemo = turn.tool_executions.some((item) => item.tool === 'portfolio_context')

  if (!symbol && !window && !informationDate && !isDemo) return null

  return (
    <dl className="mt-3 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 text-note">
      {symbol && (
        <>
          <dt className="text-faint">Company</dt>
          <dd className="text-dim">{symbol}</dd>
        </>
      )}
      {window && (
        <>
          <dt className="text-faint">{DATE_LABELS.start_date}</dt>
          <dd className="text-dim">{window}</dd>
        </>
      )}
      {informationDate && (
        <>
          <dt className="text-faint">{DATE_LABELS.information_date}</dt>
          <dd className="text-dim">{informationDate}</dd>
        </>
      )}
      {isDemo && (
        <>
          <dt className="text-faint">Portfolio values</dt>
          <dd className="text-dim">
            Demo portfolio and fictional demo prices — not a live or historical valuation.
          </dd>
        </>
      )}
    </dl>
  )
}

function FilingDetails({ filing }: { filing: FilingCitation }) {
  const rows: [string, string][] = []
  if (filing.form_type) rows.push(['Form', filing.form_type])
  if (filing.accession_number) rows.push(['Accession', filing.accession_number])
  if (filing.section) rows.push(['Section', filing.section])
  if (filing.document_name) rows.push(['Document', filing.document_name])
  if (filing.acceptance_datetime) rows.push(['Accepted', filing.acceptance_datetime])
  if (filing.report_date) rows.push(['Period', filing.report_date])
  if (filing.similarity !== null && filing.similarity !== undefined) {
    // Named a similarity, never a confidence: cosine closeness is not a probability and
    // presenting it as one invites a reading the number cannot support.
    rows.push(['Similarity', `${filing.similarity.toFixed(4)} (not a probability)`])
  }

  return (
    <>
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-label">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-faint">{label}</dt>
            <dd className="break-all text-dim">{value}</dd>
          </div>
        ))}
      </dl>
      {filing.source_url && (
        // The URL comes from the evidence record, never assembled from the answer's prose.
        <a
          href={filing.source_url}
          target="_blank"
          rel="noreferrer noopener"
          className="mt-2 inline-block text-label text-accent-ink underline underline-offset-2"
        >
          Open the filing on sec.gov
        </a>
      )}
    </>
  )
}

function EvidenceEntry({ id, item }: { id: string; item: Evidence }) {
  const quoted =
    item.kind === 'filing_passage' && typeof item.summary?.quoted_text === 'string'
      ? (item.summary.quoted_text as string)
      : null

  return (
    <li id={id} className="scroll-mt-4 rounded-md border border-line bg-page/40 p-3">
      <p className="text-label font-medium text-accent-ink">
        {item.reference} · {item.label}
      </p>
      <p className="mt-0.5 text-label text-faint">
        {item.tool}
        {item.status !== 'ok' && ` · ${item.status}`}
        {item.trimmed && ' · shortened to fit the context budget'}
      </p>

      {quoted && (
        <blockquote className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words border-l-2 border-l-line pl-3 text-note text-dim">
          {quoted}
        </blockquote>
      )}

      {item.citation && (
        <div className="mt-2">
          <FilingDetails filing={item.citation} />
        </div>
      )}

      {!quoted && item.kind === 'tool_result' && (
        <p className="mt-2 text-label text-faint">
          A structured result from {item.tool}. Its warnings and status appear under the
          limitations and tool details above.
        </p>
      )}
    </li>
  )
}

function ToolDetails({ turn }: { turn: Turn }) {
  const usage = turn.usage
  return (
    <details className="mt-3">
      <summary className="cursor-pointer text-label text-faint hover:text-dim">
        Tool details and usage
      </summary>
      <ul className="mt-2 space-y-1">
        {turn.tool_executions.map((item, index) => (
          <li key={`${item.tool}-${index}`} className="text-label text-dim">
            <span className="text-ink">{item.tool}</span>
            {item.status ? ` · ${item.status}` : ''}
            {item.reason ? ` · ${item.reason}` : ''}
            {item.reused_previous_result ? ' · reused an earlier identical call' : ''}
            {item.rejected ? ` · refused: ${item.rejected}` : ''}
            {item.evidence_refs.length > 0 ? ` · ${item.evidence_refs.join(', ')}` : ''}
          </li>
        ))}
        {turn.tool_executions.length === 0 && (
          <li className="text-label text-faint">No tools were called for this answer.</li>
        )}
      </ul>
      {usage && (
        <p className="mt-2 text-label text-faint">
          {usage.model_requests ?? 0} model requests · {usage.tool_calls ?? 0} tool calls ·{' '}
          {usage.total_tokens ?? 0} tokens
          {usage.elapsed_seconds !== undefined && ` · ${usage.elapsed_seconds}s`}
        </p>
      )}
    </details>
  )
}

export function AssistantAnswer({ turn }: { turn: Turn }) {
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const asideRef = useRef<HTMLElement>(null)

  const openEvidence = useCallback(
    (reference: string) => {
      setSourcesOpen(true)
      // After the details has opened, so the element exists to scroll to.
      window.requestAnimationFrame(() => {
        document.getElementById(evidenceId(turn.turn_id, reference))?.scrollIntoView({
          block: 'nearest',
        })
      })
    },
    [turn.turn_id],
  )

  const available = new Set(turn.evidence.map((item) => item.reference))
  const markdown = turn.answer ? withCitationLinks(turn.answer, available, turn.turn_id) : ''

  return (
    <Card>
      {turn.answer ? (
        <div className="max-w-[72ch] space-y-3 text-body text-dim [&_a]:text-accent-ink [&_a]:underline [&_code]:rounded [&_code]:bg-page [&_code]:px-1 [&_h1]:text-heading [&_h1]:font-semibold [&_h1]:text-ink [&_h2]:mt-4 [&_h2]:text-heading [&_h2]:font-semibold [&_h2]:text-ink [&_h3]:mt-3 [&_h3]:font-semibold [&_h3]:text-ink [&_li]:ml-4 [&_li]:list-disc [&_ol>li]:list-decimal [&_strong]:text-ink">
          <Markdown
            remarkPlugins={[remarkGfm]}
            // Raw HTML is not rendered at all: the answer is model output, and anything that
            // reached it through a filing passage is text, not markup.
            skipHtml
            urlTransform={(url) => safeHref(url) ?? ''}
            components={{
              a({ href, children }) {
                const safe = safeHref(href)
                if (safe?.startsWith('#evidence-')) {
                  const reference = children?.toString() ?? ''
                  return <Citation reference={reference} onOpen={openEvidence} />
                }
                if (!safe) return <span>{children}</span>
                return (
                  <a href={safe} target="_blank" rel="noreferrer noopener">
                    {children}
                  </a>
                )
              },
              // Tables scroll inside their own box rather than widening the page.
              table({ children }) {
                return (
                  <div className="overflow-x-auto">
                    <table className="w-full border-collapse text-note">{children}</table>
                  </div>
                )
              },
              th({ children }) {
                return (
                  <th className="border-b border-line px-2 py-1 text-left text-label text-faint">
                    {children}
                  </th>
                )
              },
              td({ children }) {
                return <td className="border-b border-line/60 px-2 py-1">{children}</td>
              },
            }}
          >
            {markdown}
          </Markdown>
        </div>
      ) : (
        <p className="text-body text-dim">
          {turn.failure ?? 'This turn produced no answer text.'}
        </p>
      )}

      <ContextSummary turn={turn} />

      {turn.limitations.length > 0 && (
        <div className="mt-4 rounded-md border border-line bg-page/40 p-3">
          <p className="text-label font-medium text-ink">Limitations</p>
          <ul className="mt-1 space-y-1">
            {turn.limitations.map((item) => (
              <li key={item} className="text-note text-dim">
                {item}
              </li>
            ))}
          </ul>
        </div>
      )}

      {turn.warnings.length > 0 && (
        <ul className="mt-3 space-y-1">
          {turn.warnings.map((item) => (
            <li key={item} className="text-note text-faint">
              {item}
            </li>
          ))}
        </ul>
      )}

      <ToolDetails turn={turn} />

      {turn.evidence.length > 0 && (
        <details
          className="mt-3"
          open={sourcesOpen}
          onToggle={(event) => setSourcesOpen(event.currentTarget.open)}
        >
          <summary className="cursor-pointer text-label text-faint hover:text-dim">
            Sources and evidence ({turn.evidence.length})
          </summary>
          <aside ref={asideRef} className="mt-2">
            <ul className="space-y-2">
              {turn.evidence.map((item) => (
                <EvidenceEntry
                  key={item.reference}
                  id={evidenceId(turn.turn_id, item.reference)}
                  item={item}
                />
              ))}
            </ul>
          </aside>
        </details>
      )}
    </Card>
  )
}
