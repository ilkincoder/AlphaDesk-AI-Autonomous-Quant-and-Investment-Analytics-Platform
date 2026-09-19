/** The analysis chat API: three routes, and the stream.
 *
 * Separate from `api.ts`, which owns the portfolio calls and the shared `request` helper.
 * The two are different domains with different failure vocabularies -- a portfolio read either
 * works or does not, while a question can come back *answered*, *needing clarification*,
 * *about a company we hold no data for*, or *failed* -- and keeping them in one file would
 * mean one file whose types describe two things.
 *
 * Every type here mirrors a schema the backend actually returns. They were read from the
 * running OpenAPI document rather than from a description of it, because the response the
 * handler builds and the schema it documents had already drifted apart once.
 */

import { ApiError } from './api'

/** What a turn's `status` can be. The run's own vocabulary, plus the store's two. */
export type TurnStatus =
  | 'completed'
  | 'clarification_needed'
  | 'company_not_stored'
  | 'unsupported_capability'
  | 'invalid_citations'
  | 'budget_exhausted'
  | 'provider_failed'
  | 'service_failed'
  | 'configuration_error'
  | 'processing'
  | 'interrupted'

/** Filing metadata, present on a passage and null on a tool result. */
export type FilingCitation = {
  accession_number: string | null
  form_type: string | null
  acceptance_datetime: string | null
  report_date: string | null
  source_url: string | null
  section: string | null
  similarity: number | null
  document_name: string | null
  document_role: string | null
  content_sha256: string | null
}

export type Citation = {
  reference: string
  tool: string
  label: string
  filing: FilingCitation | null
}

/** One citable thing. `summary` is a tool payload for a tool result, and the quoted text for
 *  a passage -- which is why it is `unknown` rather than a shape this file would have to
 *  guess at. */
export type Evidence = {
  reference: string
  kind: 'tool_result' | 'filing_passage'
  tool: string
  symbol: string
  label: string
  status: string
  summary: Record<string, unknown> | null
  citation: FilingCitation | null
  trimmed: boolean
}

export type ToolExecution = {
  tool: string
  arguments: Record<string, unknown>
  status: string | null
  reason: string | null
  evidence_refs: string[]
  rejection_code: string | null
  warnings: string[]
  reused_previous_result: boolean
  rejected: string | null
}

export type Resolved = {
  symbol: string | null
  start_date: string | null
  end_date: string | null
  as_of: string | null
  period?: string
  period_convention?: string | null
}

export type Usage = {
  model_requests?: number
  tool_calls?: number
  total_tokens?: number
  elapsed_seconds?: number
  limits?: Record<string, number>
  stopped_by?: string | null
} | null

/** One turn, as both `/analysis/chat` and the history route return it.
 *
 * The two are deliberately the same shape. `user_message` is the history route's copy of what
 * was typed and `question` is what the run was actually asked; they agree except when a reply
 * resumed an earlier question, which is exactly when a reader benefits from seeing both.
 */
export type Turn = {
  turn_id: string
  sequence: number | null
  request_id: string
  status: TurnStatus
  created_at?: string | null
  completed_at?: string | null
  run_id: string | null

  user_message?: string | null
  question?: string | null
  reference_date?: string | null
  destination?: string | null
  route_reason?: string | null
  model?: string | null

  answer: string | null
  symbol: string | null
  resolved: Resolved | null
  information_cutoff?: string | null

  citations: Citation[]
  evidence: Evidence[]
  limitations: string[]
  next_steps: string[]
  findings: string[]
  tool_executions: ToolExecution[]
  usage: Usage
  warnings: string[]

  failure: string | null
  retry_after_seconds?: number | null
}

export type ChatRequest = {
  conversation_id: string
  request_id: string
  message: string
  symbol?: string | null
  start_date?: string | null
  end_date?: string | null
  as_of?: string | null
}

export type ConversationSummary = {
  conversation_id: string
  created_at?: string | null
  updated_at?: string | null
  settled?: Partial<Resolved> | null
  pending_clarification?: {
    question: string
    reference_date: string | null
    asked_for: string | null
  } | null
  processing?: { turn_id: string; deadline: string | null } | null
}

export type ConversationHistory = ConversationSummary & {
  total_turns: number
  limit: number
  offset: number
  turns: Turn[]
}

/** One thing the run reported while it worked. Every field is something the finished result
 *  already exposes; nothing here is a stage the backend does not actually have. */
export type ProgressEvent = {
  event: 'routing' | 'tool' | 'findings' | 'composing' | 'done' | 'error'
  destination?: string
  symbol?: string | null
  resolved?: Resolved | null
  clarification_needed?: boolean
  tool?: string
  status?: string | null
  reason?: string | null
  reused?: boolean
  evidence_refs?: string[]
  findings?: number
  limitations?: number
  turn?: Turn
  detail?: string
}

/** The largest message the backend accepts, from `ChatRequest.message` in `app/schemas.py`.
 *  Duplicated here so the composer can stop a reader before the API has to reject them. */
export const MAX_MESSAGE_LENGTH = 2000

async function jsonRequest<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(url, init)
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new ApiError('Could not reach the API.', null)
  }

  if (!response.ok) {
    throw new ApiError(await detailOf(response), response.status)
  }
  return (await response.json()) as T
}

async function detailOf(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string' && body.detail !== '') return body.detail
    // FastAPI's validation errors are a list of objects, not a sentence.
    if (Array.isArray(body.detail) && body.detail.length > 0) {
      const first = body.detail[0] as { msg?: unknown; loc?: unknown[] }
      if (typeof first.msg === 'string') {
        const where = Array.isArray(first.loc) ? first.loc.join('.') : 'the request'
        return `${where}: ${first.msg}`
      }
    }
  } catch {
    // Not JSON. Fall through.
  }
  return `The API returned ${response.status}.`
}

export function createConversation(signal?: AbortSignal): Promise<ConversationSummary> {
  return jsonRequest<ConversationSummary>('/api/analysis/conversations', {
    method: 'POST',
    signal,
  })
}

export function fetchConversation(
  conversationId: string,
  options: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<ConversationHistory> {
  const query = new URLSearchParams()
  if (options.limit !== undefined) query.set('limit', String(options.limit))
  if (options.offset !== undefined) query.set('offset', String(options.offset))
  const suffix = query.toString() ? `?${query}` : ''
  return jsonRequest<ConversationHistory>(
    `/api/analysis/conversations/${encodeURIComponent(conversationId)}${suffix}`,
    { signal },
  )
}

export function sendMessage(body: ChatRequest, signal?: AbortSignal): Promise<Turn> {
  return jsonRequest<Turn>('/api/analysis/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}

/** Send a turn and report what the run is doing as it does it.
 *
 * The stream is read with `fetch` rather than `EventSource`, because `EventSource` cannot POST
 * a body and this request has one. That means parsing the frames here: `event:` and `data:`
 * lines, blank-line separated, exactly as `_sse` in `app/analysis_api.py` writes them.
 *
 * A non-2xx response is not a stream at all -- the route answers an error as ordinary JSON,
 * before any stream begins -- so it is read as one and thrown as an `ApiError`.
 */
export async function streamMessage(
  body: ChatRequest,
  onEvent: (event: ProgressEvent) => void,
  signal?: AbortSignal,
): Promise<Turn> {
  let response: Response
  try {
    response = await fetch('/api/analysis/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new ApiError('Could not reach the API.', null)
  }

  // A 202 is `ok` as far as `fetch` is concerned, and it is not a stream: the route answers
  // "this request is already running" as ordinary JSON, before any stream begins. Reading it
  // as a stream would report "the stream ended without an answer", which describes a different
  // situation -- and would hide the fact that the run is still going.
  if (response.status === 202) {
    throw new ApiError('This request is already being processed.', 202)
  }
  if (!response.ok) {
    throw new ApiError(await detailOf(response), response.status)
  }
  // A 200 that is not an event stream is a *replayed* turn: a request id already answered is
  // served from the stored result, as ordinary JSON, because there is nothing left to report
  // on. Reading it as a stream would find no frames and report "the stream ended without an
  // answer" -- for a request that has an answer, sitting right there in the body.
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.startsWith('text/event-stream')) {
    return (await response.json()) as Turn
  }
  if (!response.body) {
    throw new ApiError('The API returned no stream.', response.status)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let turn: Turn | null = null
  let failure: string | null = null

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // Frames are separated by a blank line. Anything after the last separator is a partial
      // frame and stays in the buffer until the rest of it arrives.
      let separator = buffer.indexOf('\n\n')
      while (separator !== -1) {
        const frame = buffer.slice(0, separator)
        buffer = buffer.slice(separator + 2)
        const event = parseFrame(frame)
        if (event) {
          onEvent(event)
          if (event.event === 'done' && event.turn) turn = event.turn
          if (event.event === 'error') failure = event.detail ?? 'The analysis failed.'
        }
        separator = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.cancel().catch(() => {
      // The stream is already gone. Nothing to do and nothing to report.
    })
  }

  if (turn) return turn
  throw new ApiError(failure ?? 'The stream ended without an answer.', null)
}

function parseFrame(frame: string): ProgressEvent | null {
  const dataLines = frame
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())

  if (dataLines.length === 0) return null
  try {
    return JSON.parse(dataLines.join('\n')) as ProgressEvent
  } catch {
    return null
  }
}
