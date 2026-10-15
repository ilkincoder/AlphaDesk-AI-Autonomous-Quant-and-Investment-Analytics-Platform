/** One assistant turn: the answer, what it rests on, and what it cannot say.
 *
 * The answer is Markdown because that is what the Supervisor writes -- headings, bold, lists
 * and tables, which is the shape a financial explanation actually takes. `react-markdown`
 * renders it with raw HTML **off**: the text is model output, and model output is not a
 * document anybody vetted. Links are restricted to `https:` and in-page anchors, so a
 * `javascript:` URL in a passage cannot become a click.
 *
 * **Reference markers are stripped, not rendered.** The Supervisor used to be asked to write
 * `[E1]` after the claims it rested on, and this component turned each one into a control that
 * jumped to the source it named. They are gone from the answer now: the sources are printed in
 * full underneath it, and a second copy of that list, spread one marker at a time through the
 * prose, was noise in the middle of the answer. What is left is removed here rather than shown,
 * because a prompt is a request -- the reader should not be the one who finds out it was
 * ignored.
 *
 * Every source link is read out of the evidence record, never out of the answer's prose. A
 * passage names its filing in `citation`; a structured result names them inside its own payload,
 * which `sourceLinks` reads by key.
 */

import type { ReactNode } from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

import type { Evidence, FilingCitation, Turn } from '../analysisApi'
import { Card } from './Card'

/** Links the answer may contain. Anything else renders as plain text.
 *
 * `https:` is allowed because SEC source links are https. `http:`, `javascript:`, `data:` and
 * the rest are not: a filing passage is untrusted text, and one of them turning into a live
 * link is exactly the thing this prevents. In-page anchors are not allowed either: nothing
 * rendered here is a link to another part of itself any more.
 */
function safeHref(url: string | undefined): string | undefined {
  if (!url) return undefined
  if (url.startsWith('https://')) return url
  return undefined
}

/** The Supervisor's reference markers -- `[E1]`, `[E2]` -- and the space in front of them.
 *
 * The space goes with the marker, because the marker sits where a claim ends: removing only the
 * brackets would turn "insiders were sellers [E1]." into "insiders were sellers ." -- a gap
 * before the full stop, and a visible trace of the thing that was taken out.
 */
const REFERENCE_MARKER = / *\[E\d+\]/g

/** Where a tool payload records the filing a row came from.
 *
 * Matched by key rather than by looking for anything that resembles a link: these are the names
 * the tools give their provenance, and a string that merely *looks* like a URL is not a source.
 * `source_reference` is `company_financial_facts`'s name for what the other two call a URL.
 */
const SOURCE_URL_KEYS = new Set(['source_url', 'source_reference', 'source_document_url'])

/** How many filings one tool result links to before the rest are counted rather than listed.
 *  An insider analysis can cover fifty transactions, and fifty links is a wall, not a source
 *  list. The remainder is still counted on screen: a short list must not read as the whole one.
 */
const MAX_SOURCE_LINKS = 5

/** One document inside an SEC filing's archive directory. */
const SEC_ARCHIVE_DOCUMENT = /^https:\/\/www\.sec\.gov\/Archives\/edgar\/data\/[^/]+\/[^/]+\/[^/]+$/i

/** The page to send a reader to for one source document.
 *
 * A filing's primary document is whatever SEC stores, and for a Form 4 that is XML -- a browser
 * shows it as a raw tree, so the link works and is useless. SEC publishes a readable page for
 * the filing in the same directory, listing the form type and a rendered version of it, and that
 * is what a reader wants. An `.htm` document is left alone: it already opens as the filing.
 */
function readerUrl(url: string, accession: unknown): string {
  if (!/\.xml$/i.test(url) || typeof accession !== 'string') return url
  if (!SEC_ARCHIVE_DOCUMENT.test(url)) return url
  return `${url.slice(0, url.lastIndexOf('/'))}/${accession}-index.htm`
}

/** The filings one tool result rests on: every source URL its payload carries.
 *
 * A passage is one filing, but a structured result is usually several -- a Form 4 per
 * transaction, a 10-K or 10-Q per observation -- so these are collected per URL rather than per
 * entry, and deduplicated, because many rows cite the same document. Each is labelled with the
 * accession recorded beside it, which is what identifies a filing to a reader. A URL that is not
 * `https:` is dropped, not rendered: nothing here is more trusted than a passage's prose.
 */
function sourceLinks(summary: Record<string, unknown> | null): { url: string; label: string }[] {
  const found: { url: string; label: string }[] = []
  const seen = new Set<string>()

  const visit = (value: unknown) => {
    if (Array.isArray(value)) {
      value.forEach(visit)
      return
    }
    if (!value || typeof value !== 'object') return
    const record = value as Record<string, unknown>
    for (const [key, child] of Object.entries(record)) {
      if (SOURCE_URL_KEYS.has(key) && typeof child === 'string') {
        const accession = record.accession_number
        const url = safeHref(readerUrl(child, accession))
        if (url && !seen.has(url)) {
          seen.add(url)
          found.push({
            url,
            label: typeof accession === 'string' ? accession : 'Open the filing on sec.gov',
          })
        }
      } else {
        visit(child)
      }
    }
  }

  visit(summary)
  return found
}

function SourceLinks({ links }: { links: { url: string; label: string }[] }) {
  const shown = links.slice(0, MAX_SOURCE_LINKS)
  const hidden = links.length - shown.length

  return (
    <p className="mt-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-label">
      <span className="text-faint">Filings behind this result:</span>
      {shown.map((link) => (
        <a
          key={link.url}
          href={link.url}
          target="_blank"
          rel="noreferrer noopener"
          // The visible label is an accession number, which is not a description of what
          // pressing it does. The accessible name says so and still contains the label.
          aria-label={`Open filing ${link.label} on sec.gov`}
          className="text-accent-ink underline underline-offset-2"
        >
          {link.label}
        </a>
      ))}
      {hidden > 0 && <span className="text-faint">and {hidden} more</span>}
    </p>
  )
}

const DATE_LABELS = {
  start_date: 'Market comparison period',
  information_date: 'Overall information date',
} as const

/** The prose style, in one place: the answer and the draft of it are the same kind of text and
 *  must not drift apart, or the answer would appear to change shape as it is finished. */
const PROSE_STYLES =
  'max-w-[72ch] space-y-3 text-body text-dim [&_a]:text-accent-ink [&_a]:underline [&_code]:rounded [&_code]:bg-page [&_code]:px-1 [&_h1]:text-heading [&_h1]:font-semibold [&_h1]:text-ink [&_h2]:mt-4 [&_h2]:text-heading [&_h2]:font-semibold [&_h2]:text-ink [&_h3]:mt-3 [&_h3]:font-semibold [&_h3]:text-ink [&_li]:ml-4 [&_li]:list-disc [&_ol>li]:list-decimal [&_strong]:text-ink'

/** Markdown as this application renders it, whoever is asking.
 *
 * Raw HTML is off everywhere: this text is model output, and anything that reached it through
 * a filing passage is text, not markup. `urlTransform` restricts links to `https:` and in-page
 * anchors, so a `javascript:` URL in a passage cannot become a click -- and `link` decides
 * what an allowed link becomes, which is the one thing the answer and the draft differ on.
 */
function Prose({
  children,
  link,
}: {
  children: string
  /** What an `a` renders as. Without one, a link is shown as its text: the draft has no
   *  checked evidence behind it, so there is nothing it is entitled to link to. */
  link?: Components['a']
}) {
  return (
    <div className={PROSE_STYLES}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={(url) => safeHref(url) ?? ''}
        components={{
          a: link ?? (({ children: text }) => <span>{text}</span>),
          // Tables scroll inside their own box rather than widening the page.
          table({ children: rows }) {
            return (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-note">{rows}</table>
              </div>
            )
          },
          th({ children: heading }) {
            return (
              <th className="border-b border-line px-2 py-1 text-left text-label text-faint">
                {heading}
              </th>
            )
          },
          td({ children: cell }) {
            return <td className="border-b border-line/60 px-2 py-1">{cell}</td>
          },
        }}
      >
        {/* Stripped here rather than at the call sites, because it is a rule about this
            application's prose and not about one reader of it: the answer and the draft of it
            are the same kind of text, and a marker must not survive in either. */}
        {children.replace(REFERENCE_MARKER, '')}
      </Markdown>
    </div>
  )
}

/** The answer as it is being written, before anything has checked it.
 *
 * Deliberately not an `AssistantAnswer`: there is no turn here and no evidence, and the text
 * can still be corrected or withdrawn by the run that is writing it. It therefore carries no
 * citation controls -- a control pointing at sources nobody has checked would be a claim the
 * draft cannot support -- and it says what it is. It is replaced by the answer itself, or by
 * the reason there is none, as soon as the run finishes.
 */
export function AnswerDraft({ text }: { text: string }) {
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="text-label font-medium text-dim">Answer being written</h2>
        <span className="rounded-full bg-selected px-2.5 py-1 text-label text-accent-ink">
          Not checked yet
        </span>
      </div>
      <p className="mt-1 max-w-[70ch] text-label text-faint">
        This is the answer as the model writes it. Its sources have not been checked yet, so it
        can still change, or be withdrawn, before the run finishes.
      </p>
      <div className="mt-3">
        <Prose>{text}</Prose>
      </div>
    </Card>
  )
}

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

function EvidenceEntry({ item }: { item: Evidence }) {
  const quoted =
    item.kind === 'filing_passage' && typeof item.summary?.quoted_text === 'string'
      ? (item.summary.quoted_text as string)
      : null
  // A passage carries its filing in `citation`, which `FilingDetails` already links. A tool
  // result carries none, and names its filings inside its own payload instead.
  const links = item.kind === 'tool_result' ? sourceLinks(item.summary) : []

  return (
    <li className="rounded-md border border-line bg-page/40 p-3">
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

      {links.length > 0 && <SourceLinks links={links} />}
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
  /** A link the answer is allowed to carry stays a link; anything else is shown as its text. */
  function AnswerLink({ href, children }: { href?: string; children?: ReactNode }) {
    const safe = safeHref(href)
    if (!safe) return <span>{children}</span>
    return (
      <a href={safe} target="_blank" rel="noreferrer noopener">
        {children}
      </a>
    )
  }

  return (
    <Card>
      {turn.answer ? (
        <Prose link={AnswerLink}>{turn.answer}</Prose>
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
        <details className="mt-3">
          <summary className="cursor-pointer text-label text-faint hover:text-dim">
            Sources and evidence ({turn.evidence.length})
          </summary>
          <aside className="mt-2">
            <ul className="space-y-2">
              {turn.evidence.map((item) => (
                <EvidenceEntry key={item.reference} item={item} />
              ))}
            </ul>
          </aside>
        </details>
      )}
    </Card>
  )
}
