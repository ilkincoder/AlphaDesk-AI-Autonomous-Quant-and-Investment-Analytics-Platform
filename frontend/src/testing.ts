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
import type {
  BacktestInstrument,
  BacktestRun,
  BacktestStrategy,
  ExecutionBatch,
  OrderIntent,
  ProposalDecision,
  RebalanceProposal,
} from './api'

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
    // Undecided, which is what a freshly generated proposal is. A test that wants a decision
    // passes one in: `aDecision()`.
    decision: null,
    batch_id: null,
    ...overrides,
  }
}

/** A recorded decision, and the exact orders it bound to.
 *
 * Undecided is the default everywhere else, so this is passed in explicitly by the tests about
 * approving and rejecting.
 */
export function aDecision(overrides: Partial<ProposalDecision> = {}): ProposalDecision {
  return {
    decision_id: 'd'.repeat(32),
    proposal_id: 'a'.repeat(32),
    decision: 'approved',
    decided_at: '2026-09-23T12:05:00+00:00',
    plan_fingerprint: 'f'.repeat(64),
    price_drift_percent: '0.0000',
    reason: null,
    order_plan: [
      {
        sequence: 1,
        symbol: 'AAPL',
        side: 'sell',
        quantity: '5',
        estimated_price: '200.00',
        estimated_value: '1000.00',
        quantity_before: '50',
      },
    ],
    ...overrides,
  }
}

/** A batch and its orders, as the Orders page reads them. */
export function aBatch(overrides: Partial<ExecutionBatch> = {}): ExecutionBatch {
  return {
    batch_id: 'b'.repeat(32),
    proposal_id: 'a'.repeat(32),
    status: 'submitting',
    created_at: '2026-09-23T12:05:00+00:00',
    started_at: '2026-09-23T12:05:01+00:00',
    completed_at: null,
    currency: 'USD',
    cash_at_approval: '1000.00',
    reserved_cash: '18.00',
    price_drift_percent: '0.0000',
    notes: ['Whole-share market orders, DAY time in force.'],
    halt_reason: null,
    sync_status: 'pending',
    sync_error: null,
    sync_at: null,
    orders: [anOrder()],
    ...overrides,
  }
}

/** One order in a batch, as the Orders page reads it. */
export function anOrder(overrides: Partial<OrderIntent> = {}): OrderIntent {
  return {
    intent_id: 'e'.repeat(32),
    sequence: 1,
    symbol: 'AAPL',
    side: 'sell',
    quantity: '5.000000',
    order_type: 'market',
    time_in_force: 'day',
    status: 'filled',
    broker_status: 'filled',
    estimated_price: '200.00',
    quantity_before: '50.000000',
    filled_quantity: '5.000000',
    filled_avg_price: '199.50',
    client_order_id: 'ad-0123456789abcdef-001',
    broker_order_id: 'broker-1',
    submitted_at: '2026-09-23T12:05:02+00:00',
    resolved_at: '2026-09-23T12:05:03+00:00',
    error: null,
    ...overrides,
  }
}

/** A strategy, as `GET /backtest/strategies` returns one.
 *
 * Copied from a real response, so the fields the page reads are the fields the API sends --
 * including the bounds, which are the API's own numbers rather than anything the page chose.
 */
export function aStrategy(overrides: Partial<BacktestStrategy> = {}): BacktestStrategy {
  return {
    id: 'moving_average_crossover',
    version: '1',
    name: 'Moving Average Crossover',
    summary:
      'Buys when a fast simple moving average crosses above a slow one, and sells when it crosses back below.',
    rules: [
      'Both averages are simple moving averages of daily closing prices.',
      'A bullish crossover (fast crossing above slow) is a buy signal while flat.',
    ],
    execution: [
      "A signal executes at the next session's open, never at the close that produced it.",
      'A final position still open at the end is marked to the last close, not sold.',
    ],
    parameters: [
      { name: 'fast_period', label: 'Fast period (sessions)', default: 20, minimum: 2, maximum: 400 },
      { name: 'slow_period', label: 'Slow period (sessions)', default: 50, minimum: 2, maximum: 400 },
    ],
    min_commission_per_share: '0',
    max_commission_per_share: '1.00',
    min_slippage_percent: '0',
    max_slippage_percent: '5.00',
    max_initial_capital: '100000000.00',
    ...overrides,
  }
}

/** The RSI strategy, with Wilder's defaults.
 *
 * Its three parameters are here rather than a period pair, which is the point: the catalogue
 * describes a strategy's own settings, and the page renders whatever it is given.
 */
export function anRsiStrategy(overrides: Partial<BacktestStrategy> = {}): BacktestStrategy {
  return aStrategy({
    id: 'rsi_reversion',
    name: 'RSI Reversion',
    summary: 'Buys when Wilder’s RSI falls through an oversold threshold.',
    parameters: [
      { name: 'period', label: 'RSI period (sessions)', default: 14, minimum: 2, maximum: 400 },
      { name: 'entry_threshold', label: 'Entry threshold (buy below this RSI)', default: 30, minimum: 1, maximum: 99 },
      { name: 'exit_threshold', label: 'Exit threshold (sell above this RSI)', default: 70, minimum: 1, maximum: 99 },
    ],
    ...overrides,
  })
}

/** One resolved listing, as a search result. */
export function anInstrument(
  overrides: Partial<BacktestInstrument> = {},
): BacktestInstrument {
  return {
    symbol: 'NVDA',
    name: 'NVIDIA Corporation',
    exchange: 'NASDAQ',
    instrument_type: 'Common Stock',
    currency: 'USD',
    country: 'United States',
    ...overrides,
  }
}

/** A completed run, as `POST /backtest/runs` returns one.
 *
 * Every number is a string, exactly as the API sends it, and the two curves are the same length
 * with the same dates because that is what a run produces: one point per session, plus the
 * opening balance.
 */
export function aBacktestRun(overrides: Partial<BacktestRun> = {}): BacktestRun {
  return {
    run_id: 'run-1',
    request_id: 'req-1',
    created_at: '2026-10-10T12:00:00+00:00',
    strategy_id: 'moving_average_crossover',
    strategy_version: '1',
    strategy_name: 'Moving Average Crossover',
    symbol: 'NVDA',
    exchange: 'NASDAQ',
    instrument_name: 'NVIDIA Corporation',
    instrument_type: 'Common Stock',
    currency: 'USD',
    start_date: '2024-01-05',
    end_date: '2024-01-12',
    initial_capital: '1000.00',
    parameters: { fast_period: 2, slow_period: 3 },
    commission_per_share: '0.005',
    slippage_percent: '0.05',
    provider: 'twelve_data',
    adjustment_basis: 'adjusted',
    provider_adjust_mode: 'splits',
    data_retrieved_at: '2026-10-10T12:00:00+00:00',
    coverage: {
      first_session: '2024-01-05',
      last_session: '2024-01-12',
      sessions: 3,
      warmup_sessions: 4,
      data_first_date: '2024-01-01',
      data_last_date: '2024-01-12',
      data_sessions: 10,
    },
    equity: [
      { on: '2024-01-05', equity: '1000.00' },
      { on: '2024-01-08', equity: '999.14' },
      { on: '2024-01-12', equity: '714.43' },
    ],
    benchmark_equity: [
      { on: '2024-01-05', equity: '1000.00' },
      { on: '2024-01-08', equity: '1165.08' },
      { on: '2024-01-12', equity: '833.08' },
    ],
    trades: [
      {
        executed_on: '2024-01-08',
        side: 'buy',
        quantity: 71,
        price: '14.0070',
        gross_value: '994.50',
        commission: '0.36',
        cash_after: '5.14',
        signalled_on: '2024-01-05',
      },
      {
        executed_on: '2024-01-12',
        side: 'sell',
        quantity: 71,
        price: '9.9950',
        gross_value: '709.65',
        commission: '0.36',
        cash_after: '714.43',
        signalled_on: '2024-01-11',
      },
    ],
    performance: {
      final_value: '714.43',
      total_return_percent: '-28.56',
      max_drawdown_percent: '37.39',
    },
    benchmark: {
      final_value: '833.08',
      total_return_percent: '-16.69',
      max_drawdown_percent: '37.41',
    },
    cash_remaining: '714.43',
    open_position: null,
    assumptions: [
      "Signals execute at the next session's open, never at the close that produced them.",
      'Commission is 0.005 USD per share, charged on each side of a trade.',
    ],
    limitations: [
      'Prices are split-adjusted and not dividend-adjusted, so every figure here is a price return.',
      'One instrument (NVDA), long only, fully invested or flat.',
    ],
    // Null is the ordinary state: a run is complete without an explanation, and one is only
    // written when it is asked for.
    explanation: null,
    explanation_model: null,
    explanation_prompt_version: null,
    ...overrides,
  }
}
