/** Fixtures for the analysis tests: a mocked API, and the shapes the backend really returns.
 *
 * The turn fixtures are copied from a live response, not invented -- including the parts that
 * are awkward, such as `filing` being null on a tool result and populated on a passage. A
 * fixture that is tidier than the API is a fixture that tests a page nobody will see.
 */

import { vi } from 'vitest'

import type { ConversationHistory, Turn } from './analysisApi'

/** A JSON response, as `fetch` would give it. */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** A server-sent-events response, framed exactly as `_sse` writes them. */
export function sseResponse(frames: object[]): Response {
  const text = frames
    .map((frame) => {
      const kind = (frame as { event?: string }).event ?? 'message'
      return `event: ${kind}\ndata: ${JSON.stringify(frame)}\n\n`
    })
    .join('')

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text))
      controller.close()
    },
  })

  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

/** A stream that delivers its frames slowly, for watching progress arrive. */
export function slowSseResponse(frames: object[], gapMs = 5): Response {
  let index = 0
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (index >= frames.length) {
        controller.close()
        return
      }
      const frame = frames[index++]
      const kind = (frame as { event?: string }).event ?? 'message'
      controller.enqueue(
        new TextEncoder().encode(`event: ${kind}\ndata: ${JSON.stringify(frame)}\n\n`),
      )
      await new Promise((resolve) => setTimeout(resolve, gapMs))
    },
  })
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

/** A turn, with everything a real one carries. */
export function aTurn(overrides: Partial<Turn> = {}): Turn {
  return {
    turn_id: 'turn-1',
    sequence: 1,
    request_id: 'req-1',
    status: 'completed',
    created_at: '2026-09-19T18:18:00Z',
    completed_at: '2026-09-19T18:18:20Z',
    run_id: 'run-turn-1',
    user_message: "Compare NVDA's price movement with insider activity.",
    question: "Compare NVDA's price movement with insider activity.",
    reference_date: '2026-09-19',
    destination: 'module1_analysis',
    route_reason: 'the question is about stored company data',
    model: 'deepseek-flash',
    answer: 'NVDA rose slightly [E1].',
    symbol: 'NVDA',
    resolved: {
      symbol: 'NVDA',
      start_date: '2026-08-06',
      end_date: '2026-09-17',
      as_of: '2026-09-19',
    },
    information_cutoff: '2026-09-20T04:00:00Z',
    citations: [
      { reference: 'E1', tool: 'market_insider_analysis', label: 'market result', filing: null },
    ],
    evidence: [
      {
        reference: 'E1',
        kind: 'tool_result',
        tool: 'market_insider_analysis',
        symbol: 'NVDA',
        label: 'market_insider_analysis result for NVDA',
        status: 'ok',
        summary: { sample_comparison: 'price_up_net_selling' },
        citation: null,
        trimmed: false,
      },
    ],
    limitations: ['insufficient_coverage'],
    next_steps: [],
    findings: [],
    tool_executions: [
      {
        tool: 'market_insider_analysis',
        arguments: { symbol: 'NVDA' },
        status: 'ok',
        reason: null,
        evidence_refs: ['E1'],
        rejection_code: null,
        warnings: [],
        reused_previous_result: false,
        rejected: null,
      },
    ],
    usage: { model_requests: 5, tool_calls: 1, total_tokens: 100, elapsed_seconds: 20 },
    warnings: [],
    failure: null,
    retry_after_seconds: null,
    ...overrides,
  }
}

/** A history page, as `GET /analysis/conversations/{id}` returns one. */
export function aHistory(
  turns: Turn[],
  overrides: Partial<ConversationHistory> = {},
): ConversationHistory {
  return {
    conversation_id: overrides.conversation_id ?? 'conv-1',
    created_at: '2026-09-19T18:00:00Z',
    updated_at: '2026-09-19T18:18:00Z',
    settled: { symbol: 'NVDA', start_date: '2026-08-06', end_date: '2026-09-17', as_of: '2026-09-19' },
    pending_clarification: null,
    processing: null,
    total_turns: turns.length,
    limit: 20,
    offset: 0,
    turns,
    ...overrides,
  }
}

export function aConversation(id = 'conv-1') {
  return {
    conversation_id: id,
    created_at: '2026-09-19T18:00:00Z',
    updated_at: '2026-09-19T18:00:00Z',
    settled: {},
    pending_clarification: null,
    processing: null,
  }
}

/** Route `fetch` by URL and method, so a test states the API rather than the call order. */
export type Route = {
  match: (url: string, init?: RequestInit) => boolean
  respond: (url: string, init?: RequestInit) => Response | Promise<Response>
}

export function routeFetch(routes: Route[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString()
    for (const route of routes) {
      if (route.match(url, init)) return route.respond(url, init)
    }
    throw new Error(`no test route for ${init?.method ?? 'GET'} ${url}`)
  })
}

export function on(method: string, fragment: string, respond: Route['respond']): Route {
  return {
    match: (url, init) =>
      (init?.method ?? 'GET').toUpperCase() === method.toUpperCase() && url.includes(fragment),
    respond,
  }
}
