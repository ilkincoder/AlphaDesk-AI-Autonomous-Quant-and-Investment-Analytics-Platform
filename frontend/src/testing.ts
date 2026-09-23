/** Fixtures shared by the frontend tests: a mocked API, and the shapes the backend really
 *  returns.
 *
 * The turn fixtures are copied from a live response, not invented -- including the parts that
 * are awkward, such as `filing` being null on a tool result and populated on a passage. A
 * fixture that is tidier than the API is a fixture that tests a page nobody will see. The
 * proposal fixture follows the same rule: it is the shape `RebalanceProposalOut` really
 * serialises, decimal strings included, rather than a tidied version of it.
 */

import { vi } from 'vitest'

import type { ConversationHistory, Turn } from './analysisApi'
import type { RebalanceProposal } from './api'

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

/** A proposal, as `GET /rebalance/proposal` returns one.
 *
 * Every number is a string, exactly as the API sends it, and the two allocations carry every
 * holding *and* cash so each side is drawn as its own complete ring. The two sides happen to
 * total the same here, and they are still kept as separate blocks for the same reason the API
 * keeps them separate: sharing a denominator is not something either side is entitled to assume.
 */
export function aProposal(overrides: Partial<RebalanceProposal> = {}): RebalanceProposal {
  const before = {
    total_value: '15500.00',
    cash_balance: '1000.00',
    cash_allocation_percent: '6.45',
    holdings: [
      {
        symbol: 'AAPL',
        quantity: '50.000000',
        price: '200.00',
        holding_value: '10000.00',
        allocation_percent: '64.52',
      },
      {
        symbol: 'MSFT',
        quantity: '10.000000',
        price: '450.00',
        holding_value: '4500.00',
        allocation_percent: '29.03',
      },
    ],
  }

  const after = {
    total_value: '15500.00',
    cash_balance: '3400.00',
    cash_allocation_percent: '21.94',
    holdings: [
      {
        symbol: 'AAPL',
        quantity: '45.000000',
        price: '200.00',
        holding_value: '9000.00',
        allocation_percent: '58.06',
      },
      {
        symbol: 'MSFT',
        quantity: '10.000000',
        price: '450.00',
        holding_value: '4500.00',
        allocation_percent: '29.03',
      },
    ],
  }

  return {
    proposal_id: 'a'.repeat(32),
    status: 'proposed',
    created_at: '2026-09-23T12:00:00+00:00',
    completed_at: '2026-09-23T12:01:10+00:00',
    freshness: 'current',
    freshness_reason: null,
    freshness_changed: [],
    snapshot: {
      read_at: '2026-09-23T12:00:00+00:00',
      price_source: 'demo',
      last_synced_at: null,
      currency: 'USD',
      cash_balance: '1000.00',
      holdings_value: '14500.00',
      total_value: '15500.00',
      reported_total_value: null,
      cash_allocation_percent: '6.45',
      positions: before.holdings,
    },
    policy: {
      name: 'alphadesk_demo_whole_share',
      description: 'A demo policy, not a stated risk preference and not an optimum.',
      constraints: ['Existing long stock holdings plus cash only.'],
      scenario: { description: 'The largest holding falls 10 percent.', shock_percent: '-10' },
    },
    calculation: {
      outcome: 'proposed',
      trades: [
        {
          symbol: 'AAPL',
          action: 'sell',
          quantity: '5',
          reference_price: '200.00',
          estimated_value: '1000.00',
          quantity_before: '50.000000',
          quantity_after: '45.000000',
        },
        {
          symbol: 'MSFT',
          action: 'buy',
          quantity: '2',
          reference_price: '450.00',
          estimated_value: '900.00',
          quantity_before: '10.000000',
          quantity_after: '12.000000',
        },
      ],
      targets: { AAPL: '0.58', MSFT: '0.29' },
      allocations: [
        {
          symbol: 'AAPL',
          current_percent: '64.52',
          target_percent: '58.00',
          achieved_percent: '58.06',
          movement: 'decrease',
          reason: 'The retrieved coverage points to a smaller position in AAPL.',
          evidence_refs: ['N1'],
        },
        {
          symbol: 'MSFT',
          current_percent: '29.03',
          target_percent: '29.00',
          achieved_percent: '29.03',
          movement: 'retain',
          reason: 'Kept where it is: the coverage says nothing that argues for moving it.',
          evidence_refs: [],
        },
        {
          symbol: 'CASH',
          current_percent: '6.45',
          target_percent: '13.00',
          achieved_percent: '12.91',
          movement: 'increase',
          reason: 'The remainder after the target weights.',
          evidence_refs: [],
        },
      ],
      cash_target: '2015.00',
      before,
      after,
      cash_before: '1000.00',
      cash_after: '1100.00',
      sell_proceeds: '1000.00',
      buy_cost: '900.00',
      buys_depend_on_sells: false,
      largest_before: { symbol: 'AAPL', allocation_percent: '64.52' },
      largest_after: { symbol: 'AAPL', allocation_percent: '58.06' },
      scenario: {
        symbol: 'AAPL',
        price_change_percent: '-10',
        price_before: '200.00',
        price_after: '180.00',
        holding_value_before: '10000.00',
        holding_value_after: '9000.00',
        total_value_before: '15500.00',
        total_value_after: '14500.00',
        change_value: '-1000.00',
        change_percent: '-6.45',
      },
      reconciliation: {
        reported_total_value: null,
        summed_total_value: '15500.00',
        difference: null,
        note: 'This portfolio has no broker-reported equity.',
      },
    },
    evidence: [
      {
        reference: 'N1',
        article_id: 7,
        title: 'Analyst Sees More Upside for Microsoft',
        publisher: 'benzinga',
        url: 'https://www.benzinga.com/story/1',
        published_at: '2026-09-22T14:00:00+00:00',
        category: 'company',
        symbols: ['MSFT'],
        similarity: 0.71,
        recent: true,
      },
    ],
    assumptions: ['Existing long stock holdings plus cash only.'],
    limitations: ['Estimated only. Nothing was sent to a broker.'],
    targets: { AAPL: '0.58', MSFT: '0.29' },
    rationale: 'The retrieved coverage points to a smaller position in AAPL.',
    failure: null,
    failure_reason: null,
    usage: { model_requests: 1, total_tokens: 1200 },
    ...overrides,
  }
}
