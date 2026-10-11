/** The Backtest page: choose a strategy, choose an asset, configure a test, run it, read it.
 *
 * **The page does no financial arithmetic.** Every figure it shows -- both curves, the return,
 * the drawdown, the trades, the cash -- is calculated in Python and arrives as decimal strings.
 * What happens here is choosing, validating and printing. That is why `Number()` appears only
 * where a geometry calculation needs it (the chart) and never on a value that is displayed.
 *
 * **The configuration survives a failure.** The form is state and a run's outcome is different
 * state, so a run that the server refused leaves every field exactly as it was. Losing a
 * twenty-field form because a date range was one day too wide is the behaviour worth designing
 * against, and it costs nothing to have.
 *
 * **One run at a time, and the guard is not only the button's `disabled`.** A second click while
 * a run is in flight is dropped rather than queued, and the same `request_id` is reused only for
 * a retry of the *same* configuration -- a changed configuration gets a new id, because it is a
 * different question and the server is entitled to answer it separately.
 *
 * **The user does not write a strategy.** The catalogue comes from the API, including the rules
 * and the execution notes it renders, so the explanation of what a strategy does cannot drift
 * from the code that runs it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  ApiError,
  explainBacktestRun,
  fetchBacktestStrategies,
  fetchLatestBacktestRun,
  fetchValuation,
  runBacktest,
  searchBacktestInstruments,
} from '../api'
import type {
  BacktestInstrument,
  BacktestRun,
  BacktestStrategy,
  ValuationHolding,
} from '../api'
import { formatCurrency, formatPercent, formatShares, PLACEHOLDER } from '../format'
import { Button } from './Button'
import { Card } from './Card'
import { EquityChart } from './EquityChart'
import { Banner, ErrorNotice, LoadingCards } from './States'

/** What the strategy catalogue is doing. */
type Catalogue =
  | { phase: 'loading' }
  | { phase: 'ready'; strategies: BacktestStrategy[] }
  | { phase: 'error'; message: string }

/** The part of a listing a run is over: the ticker and the exchange that identifies it.
 *
 * A search result carries more than this -- a name, a type, a country -- and still satisfies it.
 * A run read back from the store carries exactly these two, because they are what the row
 * records, so restoring one does not have to invent the fields it does not have. Nothing on this
 * page reads more than these two from a chosen instrument.
 */
type Listing = Pick<BacktestInstrument, 'symbol' | 'exchange'>

/** Which instrument is being backtested, and how it was chosen.
 *
 * Two arms rather than one because they carry different things. A holding gives a ticker and
 * nothing else, so the server resolves the listing; a search result *is* a listing, and the
 * exchange travels with it so the run is over the one that was picked.
 */
type Asset =
  | { kind: 'portfolio'; symbol: string }
  | { kind: 'search'; instrument: Listing }

type SearchState =
  | { phase: 'idle' }
  | { phase: 'searching' }
  | { phase: 'ready'; query: string; matches: BacktestInstrument[] }
  | { phase: 'error'; message: string }

type RunState =
  | { phase: 'idle' }
  | { phase: 'running' }
  | { phase: 'ready'; run: BacktestRun }
  | { phase: 'error'; message: string }

type Holdings =
  | { phase: 'loading' }
  | { phase: 'ready'; holdings: ValuationHolding[] }
  | { phase: 'error'; message: string }

const DEFAULT_CAPITAL = '10000.00'
// The period defaults are not here: they belong to the strategy that declares them, and the
// catalogue is where this page reads them from.
const DEFAULT_COMMISSION = '0.005'
const DEFAULT_SLIPPAGE = '0.05'

/** How far back the form starts. Three years is long enough to contain several crossovers of a
 *  50-session pair without asking the provider for a decade of bars nobody will read. */
const DEFAULT_YEARS = 3

function isoDay(at: Date): string {
  return at.toISOString().slice(0, 10)
}

function newRequestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `bt-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}

/** A money or percentage field that will survive the round trip unchanged.
 *
 * The bounds are the columns' own scales -- `NUMERIC(18,2)` for the capital, `(18,6)` for the
 * commission, `(9,4)` for the slippage. A value with more places would be *rounded on the way
 * into the database* while the run used the unrounded one, which is a row that describes an
 * arithmetic nobody performed. Refusing it here is cheaper than discovering that later.
 */
const DECIMALS = {
  capital: 2,
  commission: 6,
  slippage: 4,
} as const

function isDecimal(value: string, places: number): boolean {
  return new RegExp(`^\\d+(\\.\\d{1,${places}})?$`).test(value)
}

function isWholeNumber(value: string): boolean {
  return /^\d+$/.test(value)
}

/** Everything the form must satisfy before a request is made.
 *
 * The server checks all of it again -- this is not the guard, it is what stops an obvious typo
 * costing a provider request and a round trip. Every bound comes from the API's own catalogue
 * rather than being repeated here, so a strategy's declarations are the only place its numbers
 * are described.
 *
 * **Only the per-parameter bounds are checked here, not the relations between them.** That a
 * fast period must be shorter than a slow one, and an entry threshold below an exit threshold,
 * are rules belonging to the strategies that define them; restating them in this page would be
 * a second copy to keep in step. The server refuses a relation it does not accept, before any
 * provider request is made, and the refusal is shown like any other.
 */
function validateInput(form: Form, strategy: BacktestStrategy): string | null {
  if (!form.startDate || !form.endDate) return 'Choose both a start date and an end date.'
  if (form.startDate >= form.endDate) {
    return 'The start date must be before the end date.'
  }

  const capital = form.capital.trim()
  if (!isDecimal(capital, DECIMALS.capital) || Number(capital) <= 0) {
    return 'Enter an initial capital greater than zero, in USD, to at most two decimal places.'
  }
  if (Number(capital) > Number(strategy.max_initial_capital)) {
    return `The initial capital must be at most ${formatCurrency(strategy.max_initial_capital)}.`
  }

  for (const param of strategy.parameters) {
    const value = (form.parameters[param.name] ?? '').trim()
    if (!isWholeNumber(value)) {
      return `The ${param.label.toLowerCase()} must be a whole number.`
    }
    if (Number(value) < param.minimum || Number(value) > param.maximum) {
      return `The ${param.label.toLowerCase()} must be between ${param.minimum} and ${param.maximum}.`
    }
  }

  const commission = form.commission.trim()
  if (!isDecimal(commission, DECIMALS.commission)) {
    return 'Enter the commission as USD per share, to at most six decimal places.'
  }
  if (Number(commission) > Number(strategy.max_commission_per_share)) {
    return `The commission must be at most ${strategy.max_commission_per_share} USD per share.`
  }

  const slippage = form.slippage.trim()
  if (!isDecimal(slippage, DECIMALS.slippage)) {
    return 'Enter the slippage as a percentage, to at most four decimal places.'
  }
  if (Number(slippage) > Number(strategy.max_slippage_percent)) {
    return `The slippage must be at most ${strategy.max_slippage_percent} percent.`
  }

  return null
}

type Form = {
  startDate: string
  endDate: string
  capital: string
  /** The chosen strategy's own parameters, keyed by the names it declares and held as the
   *  text that was typed. What they are is the strategy's business: this form renders a field
   *  per declaration and never assumes there are two of them called fast and slow. */
  parameters: Record<string, string>
  commission: string
  slippage: string
}

/** The form's fields that hold one piece of text.
 *
 * `parameters` is excluded because it is not one field but a set of them, declared by the chosen
 * strategy and rendered from those declarations. Every other field is a single string with a
 * fixed name, and this is what keeps the two from being handled by the same accessor.
 */
type ScalarField = Exclude<keyof Form, 'parameters'>

function initialForm(): Form {
  const today = new Date()
  const from = new Date(today)
  from.setFullYear(from.getFullYear() - DEFAULT_YEARS)
  return {
    startDate: isoDay(from),
    endDate: isoDay(today),
    capital: DEFAULT_CAPITAL,
    // Empty until a strategy is chosen: its declarations are where the defaults come from, and
    // seeding them here would be this form guessing at a strategy it has not been told about.
    parameters: {},
    commission: DEFAULT_COMMISSION,
    slippage: DEFAULT_SLIPPAGE,
  }
}

/** A strategy's default parameters, as the text its fields start with. */
function defaultParameters(strategy: BacktestStrategy): Record<string, string> {
  return Object.fromEntries(
    strategy.parameters.map((param) => [param.name, String(param.default)]),
  )
}

/** The run's parameters as one compact line -- "20/50", "14/30/70".
 *
 * Ordered by the strategy's own declarations rather than by the document's keys: what was read
 * back from the database has no order worth relying on, and "50/20" under a heading that means
 * fast then slow would misstate which number is which. A run whose strategy is no longer
 * defined still shows its values, in whatever order they come back -- unsorted is better than
 * an order this page would be inventing.
 */
function parameterSummary(run: BacktestRun, strategy: BacktestStrategy | null): string {
  const names = strategy?.parameters.map((param) => param.name) ?? Object.keys(run.parameters)
  return names
    .map((name) => run.parameters[name])
    .filter((value) => value !== undefined)
    .join('/')
}

export function BacktestPage() {
  const [catalogue, setCatalogue] = useState<Catalogue>({ phase: 'loading' })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [asset, setAsset] = useState<Asset | null>(null)
  const [form, setForm] = useState<Form>(initialForm)
  const [result, setResult] = useState<RunState>({ phase: 'idle' })
  const [problem, setProblem] = useState<string | null>(null)
  const [restoreProblem, setRestoreProblem] = useState<string | null>(null)

  // The id of the request currently in flight, and the one the last *successful* run used. Kept
  // in a ref rather than state because neither is rendered: a second click must be blocked
  // synchronously, and a re-render is not a synchronisation primitive.
  const running = useRef(false)
  const requestId = useRef<string | null>(null)
  const runSignature = useRef<string | null>(null)

  // Whether the person has changed anything yet. Restoring a saved run reads the network, so it
  // lands after the first paint -- and a form someone has already started filling in must not be
  // overwritten by a response to a request they made before they touched it. A ref because the
  // handlers that set it run outside a render and must see the current value, not a captured one.
  const edited = useRef(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    setCatalogue({ phase: 'loading' })
    try {
      const body = await fetchBacktestStrategies(signal)
      setCatalogue({ phase: 'ready', strategies: body.strategies })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setCatalogue({ phase: 'error', message: messageOf(error) })
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  /** Put back the last run: the strategy, the instrument, the configuration and the results.
   *
   * One read and no arithmetic. The stored row already holds the resolved listing, the window,
   * the costs and the curves, so nothing is recalculated and no price is fetched again -- running
   * it afresh would spend a provider credit to arrive at the same figures, and would show today's
   * arithmetic under yesterday's evidence.
   *
   * Two conditions are checked rather than assumed. **Nothing saved** is not a failure: an empty
   * form is the right way to start, so it is passed over in silence. And a response that arrives
   * after the person has changed something is *dropped*, because a form being filled in is worth
   * more than a restore nobody asked for on this visit.
   */
  const restore = useCallback(async (signal?: AbortSignal) => {
    try {
      const { run } = await fetchLatestBacktestRun(signal)
      if (run === null || edited.current) return

      setSelectedId(run.strategy_id)
      setAsset({ kind: 'search', instrument: { symbol: run.symbol, exchange: run.exchange } })
      setForm({
        startDate: run.start_date,
        endDate: run.end_date,
        capital: run.initial_capital,
        // Whatever the run was configured with, by the names it recorded -- the strategy that
        // produced it is the only thing that knows what they are called and how many there are.
        parameters: Object.fromEntries(
          Object.entries(run.parameters).map(([name, value]) => [name, String(value)]),
        ),
        commission: run.commission_per_share,
        slippage: run.slippage_percent,
      })
      setResult({ phase: 'ready', run })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      // Said quietly rather than shown as an error. A saved run that cannot be read leaves the
      // page entirely usable -- the catalogue, the form and a fresh run all still work -- so this
      // is a note about what is missing, not a failure of the page.
      setRestoreProblem(messageOf(error))
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void restore(controller.signal)
    return () => controller.abort()
  }, [restore])

  const strategies = catalogue.phase === 'ready' ? catalogue.strategies : []
  const strategy = strategies.find((item) => item.id === selectedId) ?? null

  const field = useCallback((name: ScalarField) => ({
    value: form[name],
    onChange: (event: { target: { value: string } }) => {
      edited.current = true
      setForm((current) => ({ ...current, [name]: event.target.value }))
    },
  }), [form])

  const chooseStrategy = useCallback((chosen: BacktestStrategy) => {
    edited.current = true
    setSelectedId(chosen.id)
    // The fields start from *this* strategy's own defaults, replacing whatever the previous one
    // had. Keeping the old values would carry a fast period into an RSI run, where there is no
    // such parameter -- a request the server would refuse, for a reason the person never chose.
    setForm((current) => ({ ...current, parameters: defaultParameters(chosen) }))
    // A result belongs to the configuration that produced it. Changing the strategy does not
    // leave the old figures on screen under a form that now says something else.
    setResult({ phase: 'idle' })
    setProblem(null)
  }, [])

  const chooseAsset = useCallback((next: Asset) => {
    edited.current = true
    setAsset(next)
    setResult({ phase: 'idle' })
    setProblem(null)
  }, [])

  const change = useCallback((name: string, value: string) => {
    edited.current = true
    setForm((current) => ({
      ...current,
      parameters: { ...current.parameters, [name]: value },
    }))
  }, [])

  const canRun = strategy !== null && asset !== null && result.phase !== 'running'

  const start = useCallback(async () => {
    // The synchronous guard. `disabled` on the button is the visible half of this; a click that
    // arrives between the render and the state update is caught here.
    if (running.current || strategy === null || asset === null) return

    const complaint = validateInput(form, strategy)
    if (complaint !== null) {
      setProblem(complaint)
      return
    }

    const body = {
      strategy_id: strategy.id,
      symbol: asset.kind === 'portfolio' ? asset.symbol : asset.instrument.symbol,
      ...(asset.kind === 'search' ? { exchange: asset.instrument.exchange } : {}),
      start_date: form.startDate,
      end_date: form.endDate,
      initial_capital: form.capital.trim(),
      // Named and numbered by the strategy's own declarations, so a request carries exactly the
      // parameters that strategy reads and nothing else.
      parameters: Object.fromEntries(
        strategy.parameters.map((param) => [
          param.name,
          Number((form.parameters[param.name] ?? '').trim()),
        ]),
      ),
      costs: {
        commission_per_share: form.commission.trim(),
        slippage_percent: form.slippage.trim(),
      },
    }

    // A retry of the same configuration reuses its id, so the server answers from the run it
    // already has rather than fetching the same bars again. A *changed* configuration is a
    // different question and gets a new id.
    const signature = JSON.stringify(body)
    if (runSignature.current !== signature || requestId.current === null) {
      requestId.current = newRequestId()
      runSignature.current = signature
    }

    running.current = true
    setProblem(null)
    setResult({ phase: 'running' })

    try {
      const run = await runBacktest({ request_id: requestId.current, ...body })
      setResult({ phase: 'ready', run })
    } catch (error) {
      setResult({ phase: 'error', message: messageOf(error) })
    } finally {
      running.current = false
    }
  }, [asset, form, strategy])

  return (
    <div>
      <header>
        <h1 className="text-title font-bold text-ink">Backtest</h1>
        <p className="mt-1 text-note text-dim">
          Test a strategy against a stock&rsquo;s past prices. Nothing is traded &mdash; this
          simulates what the rules would have done.
        </p>
      </header>

      <div className="mt-6">
        {restoreProblem !== null && (
          <div className="mb-4">
            <Banner>
              The last saved run could not be loaded, so the form starts empty. Anything you run
              now is stored as usual.
            </Banner>
          </div>
        )}

        {catalogue.phase === 'loading' && <LoadingCards />}

        {catalogue.phase === 'error' && (
          <ErrorNotice
            title="Could not load the strategies."
            message={catalogue.message}
            onRetry={() => void load()}
          />
        )}

        {catalogue.phase === 'ready' && (
          <div className="grid grid-cols-1 gap-4 min-[1100px]:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <div className="space-y-4">
              <StrategyCard
                strategies={strategies}
                selectedId={selectedId}
                onSelect={chooseStrategy}
              />
              {strategy === null ? (
                <Card>
                  <h2 className="text-heading font-medium text-ink">Asset</h2>
                  <p className="mt-3 text-body text-dim">Choose a strategy first.</p>
                </Card>
              ) : (
                <AssetCard asset={asset} onChoose={chooseAsset} />
              )}

              {strategy !== null && (
                <SettingsCard
                  strategy={strategy}
                  asset={asset}
                  form={form}
                  field={field}
                  onChange={change}
                  problem={problem}
                  canRun={canRun}
                  running={result.phase === 'running'}
                  onRun={() => void start()}
                />
              )}
            </div>

            <ResultsPanel
              result={result}
              strategy={strategy}
              onDismiss={() => setResult({ phase: 'idle' })}
            />
          </div>
        )}
      </div>
    </div>
  )
}

// --- strategy ---------------------------------------------------------------------------------

function StrategyCard({
  strategies,
  selectedId,
  onSelect,
}: {
  strategies: BacktestStrategy[]
  selectedId: string | null
  // The whole strategy, not its id: choosing one is what seeds the parameter fields, and the
  // defaults it seeds them with are declared by the strategy itself.
  onSelect: (strategy: BacktestStrategy) => void
}) {
  return (
    <Card>
      <h2 className="text-heading font-medium text-ink">Strategy</h2>
      <p className="mt-1 text-label text-faint">
        Strategies are defined by AlphaDesk. Choose one to test.
      </p>

      <ul className="mt-4 space-y-2">
        {strategies.map((strategy) => {
          const chosen = strategy.id === selectedId
          return (
            <li key={strategy.id}>
              <button
                type="button"
                aria-pressed={chosen}
                onClick={() => onSelect(strategy)}
                className={`w-full rounded-md border p-3 text-left transition-colors ${
                  chosen
                    ? 'border-accent bg-selected'
                    : 'border-line bg-page hover:border-accent'
                }`}
              >
                <span className="flex items-baseline justify-between gap-2">
                  <span
                    className={`text-body font-medium ${chosen ? 'text-accent-ink' : 'text-ink'}`}
                  >
                    {strategy.name}
                  </span>
                  {/* The strategy's own defaults, stated here rather than only in the form, so
                      the choice is made with them visible. */}
                  <span className="text-label text-faint">
                    {strategy.parameters.map((param) => param.default).join('/')}
                  </span>
                </span>
                <span className="mt-1 block text-note text-dim">{strategy.summary}</span>
              </button>
            </li>
          )
        })}
      </ul>

      {strategies.length === 0 && (
        <p className="mt-3 text-body text-dim">No strategies are defined.</p>
      )}
    </Card>
  )
}

// --- asset ------------------------------------------------------------------------------------

function AssetCard({
  asset,
  onChoose,
}: {
  asset: Asset | null
  onChoose: (asset: Asset) => void
}) {
  const [tab, setTab] = useState<'portfolio' | 'search'>('portfolio')

  return (
    <Card>
      <h2 className="text-heading font-medium text-ink">Asset</h2>

      <div role="tablist" aria-label="How to choose an asset" className="mt-3 flex gap-2">
        <TabButton
          current={tab === 'portfolio'}
          onClick={() => setTab('portfolio')}
          label="From my portfolio"
        />
        <TabButton
          current={tab === 'search'}
          onClick={() => setTab('search')}
          label="Search for another stock"
        />
      </div>

      <div className="mt-4">
        {tab === 'portfolio' ? (
          <PortfolioPicker asset={asset} onChoose={onChoose} />
        ) : (
          <SearchPicker asset={asset} onChoose={onChoose} />
        )}
      </div>
    </Card>
  )
}

function TabButton({
  current,
  onClick,
  label,
}: {
  current: boolean
  onClick: () => void
  label: string
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={current}
      onClick={onClick}
      className={`h-8 rounded-md border px-3 text-label font-medium transition-colors ${
        current ? 'border-accent bg-selected text-accent-ink' : 'border-line bg-panel text-dim hover:text-ink'
      }`}
    >
      {label}
    </button>
  )
}

function PortfolioPicker({
  asset,
  onChoose,
}: {
  asset: Asset | null
  onChoose: (asset: Asset) => void
}) {
  const [state, setState] = useState<Holdings>({ phase: 'loading' })

  useEffect(() => {
    const controller = new AbortController()

    async function load(signal: AbortSignal) {
      setState({ phase: 'loading' })
      try {
        const valuation = await fetchValuation(signal)
        // Cash is not a stock and has no price series to test a moving average against.
        setState({ phase: 'ready', holdings: valuation.holdings })
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setState({
          phase: 'error',
          message:
            error instanceof ApiError && error.status === 404
              ? 'No portfolio is stored. Search for a stock instead.'
              : messageOf(error),
        })
      }
    }

    void load(controller.signal)
    return () => controller.abort()
  }, [])

  if (state.phase === 'loading') {
    return (
      <p className="text-body text-dim" role="status">
        Loading your holdings&hellip;
      </p>
    )
  }

  if (state.phase === 'error') {
    return (
      <p role="alert" className="text-body text-danger">
        {state.message}
      </p>
    )
  }

  // A cash-only portfolio is a real state, not a defensive one.
  if (state.holdings.length === 0) {
    return (
      <p className="text-body text-dim">
        This portfolio holds no stock. Search for another stock instead.
      </p>
    )
  }

  const chosen = asset?.kind === 'portfolio' ? asset.symbol : ''

  return (
    <div>
      <label htmlFor="backtest-holding" className="text-label text-faint">
        Holding
      </label>
      <select
        id="backtest-holding"
        value={chosen}
        onChange={(event) => onChoose({ kind: 'portfolio', symbol: event.target.value })}
        className="mt-1.5 h-9 w-full rounded-md border border-line bg-page px-3 text-body text-ink"
      >
        <option value="" disabled>
          Choose a holding
        </option>
        {state.holdings.map((holding) => (
          <option key={holding.symbol} value={holding.symbol}>
            {holding.symbol}
          </option>
        ))}
      </select>
      <p className="mt-1.5 text-label text-faint">
        Choosing a holding selects the stock. Its quantity and purchase price are not used: the
        test starts flat with the initial capital below.
      </p>
    </div>
  )
}

function SearchPicker({
  asset,
  onChoose,
}: {
  asset: Asset | null
  onChoose: (asset: Asset) => void
}) {
  const [query, setQuery] = useState('')
  const [state, setState] = useState<SearchState>({ phase: 'idle' })

  const search = useCallback(async (term: string) => {
    const trimmed = term.trim()
    if (trimmed === '') {
      setState({ phase: 'idle' })
      return
    }

    setState({ phase: 'searching' })
    try {
      const body = await searchBacktestInstruments(trimmed)
      setState({ phase: 'ready', query: body.query, matches: body.matches })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setState({ phase: 'error', message: messageOf(error) })
    }
  }, [])

  const selected =
    asset?.kind === 'search'
      ? `${asset.instrument.symbol} · ${asset.instrument.exchange}`
      : null

  return (
    <div>
      <label htmlFor="backtest-search" className="text-label text-faint">
        Enter a ticker symbol or company name.
      </label>
      <form
        className="mt-1.5 flex gap-2"
        onSubmit={(event) => {
          // A form so Enter submits, which is what anyone typing a ticker expects.
          event.preventDefault()
          void search(query)
        }}
      >
        <input
          id="backtest-search"
          type="text"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          autoComplete="off"
          className="h-9 min-w-0 flex-1 rounded-md border border-line bg-page px-3 text-body text-ink"
        />
        <Button type="submit" disabled={state.phase === 'searching'}>
          {state.phase === 'searching' ? 'Searching…' : 'Search'}
        </Button>
      </form>

      {state.phase === 'error' && (
        <p role="alert" className="mt-2 text-body text-danger">
          {state.message}
        </p>
      )}

      {state.phase === 'ready' && state.matches.length === 0 && (
        <p className="mt-2 text-body text-dim">
          No US-listed stock matches &ldquo;{state.query}&rdquo;. Check the spelling, or try the
          company&rsquo;s name.
        </p>
      )}

      {state.phase === 'ready' && state.matches.length > 0 && (
        <ul aria-label="Search results" className="mt-2 space-y-1">
          {state.matches.map((match) => {
            const isChosen =
              asset?.kind === 'search' &&
              asset.instrument.symbol === match.symbol &&
              asset.instrument.exchange === match.exchange
            return (
              <li key={`${match.symbol}-${match.exchange}`}>
                <button
                  type="button"
                  aria-pressed={isChosen}
                  onClick={() => onChoose({ kind: 'search', instrument: match })}
                  className={`flex w-full items-baseline justify-between gap-2 rounded-md border px-3 py-2 text-left transition-colors ${
                    isChosen ? 'border-accent bg-selected' : 'border-line bg-page hover:border-accent'
                  }`}
                >
                  <span className="min-w-0">
                    <span className="text-body font-medium text-ink">{match.symbol}</span>
                    <span className="ml-2 text-note text-dim">{match.name}</span>
                  </span>
                  {/* The exchange is part of what is being chosen: the same ticker on another
                      exchange is a different instrument with a different price series. */}
                  <span className="shrink-0 text-label text-faint">{match.exchange}</span>
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {selected !== null && (
        <p className="mt-2 text-label text-accent-ink">Selected: {selected}</p>
      )}
      {state.phase === 'ready' && state.matches.length > 0 && selected === null && (
        <p className="mt-2 text-label text-faint">Choose one of these to continue.</p>
      )}
    </div>
  )
}

// --- settings ---------------------------------------------------------------------------------

function SettingsCard({
  strategy,
  asset,
  form,
  field,
  onChange,
  problem,
  canRun,
  running,
  onRun,
}: {
  strategy: BacktestStrategy
  asset: Asset | null
  form: Form
  field: (name: ScalarField) => {
    value: string
    onChange: (event: { target: { value: string } }) => void
  }
  onChange: (name: string, value: string) => void
  problem: string | null
  canRun: boolean
  running: boolean
  onRun: () => void
}) {
  const instrument = useMemo(() => {
    if (asset === null) return null
    return asset.kind === 'portfolio'
      ? { symbol: asset.symbol, exchange: null }
      : { symbol: asset.instrument.symbol, exchange: asset.instrument.exchange }
  }, [asset])

  return (
    <Card>
      <h2 className="text-heading font-medium text-ink">Test settings</h2>

      <dl className="mt-3 space-y-1 text-note">
        <SummaryRow label="Strategy" value={strategy.name} />
        <SummaryRow
          label="Asset"
          value={
            instrument === null
              ? 'None chosen yet'
              : instrument.exchange === null
                ? instrument.symbol
                : `${instrument.symbol} · ${instrument.exchange}`
          }
        />
      </dl>

      <div className="mt-5 grid grid-cols-2 gap-3">
        <Field label="Start date" id="backtest-start">
          <input
            id="backtest-start"
            type="date"
            {...field('startDate')}
            className="h-9 w-full rounded-md border border-line bg-page px-3 text-body text-ink"
          />
        </Field>
        <Field label="End date" id="backtest-end">
          <input
            id="backtest-end"
            type="date"
            {...field('endDate')}
            className="h-9 w-full rounded-md border border-line bg-page px-3 text-body text-ink"
          />
        </Field>
      </div>

      <div className="mt-3">
        <Field label="Initial capital (USD)" id="backtest-capital">
          <input
            id="backtest-capital"
            type="text"
            inputMode="decimal"
            {...field('capital')}
            className="h-9 w-full rounded-md border border-line bg-page px-3 text-right tabular-nums text-body text-ink"
          />
        </Field>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3">
        {strategy.parameters.map((param) => (
          <Field key={param.name} label={param.label} id={`backtest-param-${param.name}`}>
            <input
              id={`backtest-param-${param.name}`}
              type="number"
              min={param.minimum}
              max={param.maximum}
              step={1}
              value={form.parameters[param.name] ?? ''}
              onChange={(event) => onChange(param.name, event.target.value)}
              className="h-9 w-full rounded-md border border-line bg-page px-3 text-right tabular-nums text-body text-ink"
            />
          </Field>
        ))}
      </div>
      <p className="mt-1.5 text-label text-faint">
        The strategy's own settings, with the defaults it suggests:{' '}
        {strategy.parameters
          .map((param) => `${param.label.split(' (')[0].toLowerCase()} ${param.default}`)
          .join(', ')}
        .
      </p>

      <details className="mt-4 border-t border-line pt-3">
        <summary className="cursor-pointer text-label font-medium text-dim">
          Advanced settings
        </summary>
        <div className="mt-3 space-y-3">
          <Field label="Commission (USD per share, each side)" id="backtest-commission">
            <input
              id="backtest-commission"
              type="text"
              inputMode="decimal"
              {...field('commission')}
              className="h-9 w-full rounded-md border border-line bg-page px-3 text-right tabular-nums text-body text-ink"
            />
          </Field>
          <Field label="Slippage (percent of price, against the trade)" id="backtest-slippage">
            <input
              id="backtest-slippage"
              type="text"
              inputMode="decimal"
              {...field('slippage')}
              className="h-9 w-full rounded-md border border-line bg-page px-3 text-right tabular-nums text-body text-ink"
            />
          </Field>
          <p className="text-label text-faint">
            Defaults: {DEFAULT_COMMISSION} USD per share and {DEFAULT_SLIPPAGE}%. A buy pays the
            session&rsquo;s open plus the slippage and a sell receives the open minus it, so the
            costs are charged on both strategies, including buy-and-hold. Commission can be up to{' '}
            {strategy.max_commission_per_share} USD per share and slippage up to{' '}
            {strategy.max_slippage_percent}%.
          </p>
        </div>
      </details>

      {problem !== null && (
        <p role="alert" className="mt-4 text-body text-danger">
          {problem}
        </p>
      )}

      <div className="mt-5 flex items-center gap-3">
        {/* aria-disabled rather than `disabled`, so a button that becomes un-runnable while
            focused does not drop the focus ring. The click guard is the ref in `start`. */}
        <Button
          variant="primary"
          onClick={onRun}
          aria-disabled={!canRun}
          className={canRun ? '' : 'opacity-50'}
        >
          {running ? 'Running…' : 'Run backtest'}
        </Button>
        {asset === null && (
          <span className="text-label text-faint">Choose an asset to run this test.</span>
        )}
      </div>
    </Card>
  )
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3">
      <dt className="text-label text-faint">{label}</dt>
      <dd className="text-body text-ink">{value}</dd>
    </div>
  )
}

function Field({
  label,
  id,
  children,
}: {
  label: string
  id: string
  children: React.ReactNode
}) {
  return (
    <div>
      <label htmlFor={id} className="text-label text-faint">
        {label}
      </label>
      <div className="mt-1.5">{children}</div>
    </div>
  )
}

// --- results ----------------------------------------------------------------------------------

function ResultsPanel({
  result,
  strategy,
  onDismiss,
}: {
  result: RunState
  strategy: BacktestStrategy | null
  onDismiss: () => void
}) {
  return (
    <Card className="flex flex-col min-[1100px]:min-h-[400px]">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h2 className="text-heading font-medium text-ink">Results</h2>
        {/* Shown only beside figures, and it stays with them. Every number below is a price
            return: the provider's bars are split-adjusted and not dividend-adjusted. */}
        {result.phase === 'ready' && (
          <span className="rounded-full bg-selected px-2.5 py-1 text-label text-accent-ink">
            Price return · simulated
          </span>
        )}
      </div>

      {result.phase === 'idle' && (
        <p className="mt-3 text-body text-dim">
          Choose a strategy and an asset, then run the test to see the results here.
        </p>
      )}

      {result.phase === 'running' && (
        <p className="mt-3 text-body text-dim" role="status">
          Reading prices and running the test&hellip;
        </p>
      )}

      {result.phase === 'error' && (
        <div className="mt-3">
          <p role="alert" className="border-l-2 border-l-danger pl-3 text-body text-ink">
            {result.message}
          </p>
          <p className="mt-2 text-label text-faint">
            Your settings are unchanged. Adjust them and run again.
          </p>
          <Button className="mt-3" onClick={onDismiss}>
            Dismiss
          </Button>
        </div>
      )}

      {result.phase === 'ready' && <RunResult run={result.run} strategy={strategy} />}
    </Card>
  )
}

function RunResult({
  run,
  strategy,
}: {
  run: BacktestRun
  strategy: BacktestStrategy | null
}) {
  const strategyName = strategy?.name ?? null
  const performance = run.performance
  const benchmark = run.benchmark
  if (performance === null) return null

  return (
    <div className="mt-4 space-y-5">
      <p className="text-note text-dim">
        <span className="font-medium text-ink">{run.symbol}</span>
        {` · ${run.exchange}`}
        {run.instrument_name !== null && ` · ${run.instrument_name}`}
        <br />
        {run.start_date} to {run.end_date} · {run.coverage?.sessions ?? 0} sessions ·{' '}
        {parameterSummary(run, strategy)}
      </p>

      <div className="grid grid-cols-2 gap-4">
        <Metric
          label="Net total return"
          value={formatPercent(performance.total_return_percent)}
          tone={signOf(performance.total_return_percent)}
        />
        <Metric label="Final value" value={formatCurrency(performance.final_value)} />
        <Metric
          label="Maximum drawdown"
          value={formatPercent(performance.max_drawdown_percent)}
        />
        <Metric label="Completed trades" value={String(completedTrades(run))} />
      </div>

      <EquityChart
        equity={run.equity}
        benchmark={run.benchmark_equity}
        strategyName={strategyName ?? run.strategy_name}
      />

      {benchmark !== null && (
        <p className="text-note text-dim">
          Buy and hold, on the same instrument and costs:{' '}
          <span className="tabular-nums text-ink">
            {formatPercent(benchmark.total_return_percent)}
          </span>{' '}
          ({formatCurrency(benchmark.final_value)}), maximum drawdown{' '}
          {formatPercent(benchmark.max_drawdown_percent)}.
        </p>
      )}

      <TradeLedger run={run} />

      <div className="border-t border-line pt-4">
        <p className="text-label text-faint">Cash and position at the end</p>
        <p className="mt-1 text-body text-ink">
          Cash remaining: {formatCurrency(run.cash_remaining ?? '0')}
          {run.open_position === null ? (
            <> · No open position</>
          ) : (
            <>
              {' '}
              · Holding {formatShares(String(run.open_position.quantity))} share
              {run.open_position.quantity === 1 ? '' : 's'}, bought {run.open_position.entry_date}{' '}
              at {formatCurrency(run.open_position.entry_price)}, marked to{' '}
              {formatCurrency(run.open_position.mark_price)} on {run.open_position.mark_date} (
              {formatCurrency(run.open_position.market_value)})
            </>
          )}
        </p>
      </div>

      <ExplainPanel key={run.run_id} run={run} />

      <Disclosures run={run} strategyName={strategyName} />
    </div>
  )
}

/** A net return is signed, so a rise and a fall are told apart by a word as well as a colour. */
function signOf(value: string): 'up' | 'down' | 'flat' {
  if (!/[1-9]/.test(value)) return 'flat'
  return value.startsWith('-') ? 'down' : 'up'
}

function completedTrades(run: BacktestRun): number {
  // A completed trade is a buy and the sell that closed it. A buy still open at the end is not
  // one, which is why this counts sells rather than rows in the ledger.
  return run.trades.filter((trade) => trade.side === 'sell').length
}

function Metric({
  label,
  value,
  tone = 'flat',
}: {
  label: string
  value: string
  tone?: 'up' | 'down' | 'flat'
}) {
  const colour = tone === 'up' ? 'text-positive' : tone === 'down' ? 'text-danger' : 'text-ink'
  return (
    <div>
      <p className="text-label text-faint">{label}</p>
      {/* The sign is in the value and the word is in the label; the colour is a third signal
          and never the only one. */}
      <p className={`mt-0.5 tabular-nums text-heading font-medium ${colour}`}>{value}</p>
    </div>
  )
}

function TradeLedger({ run }: { run: BacktestRun }) {
  if (run.trades.length === 0) {
    return (
      <div className="border-t border-line pt-4">
        <p className="text-label text-faint">Trades</p>
        <p className="mt-1 text-body text-dim">
          The strategy produced no trades over this period: no crossover occurred, or the ones
          that did happened before the test started.
        </p>
      </div>
    )
  }

  return (
    <div className="border-t border-line pt-4">
      <p className="text-label text-faint">Trades</p>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[560px] border-collapse text-note">
          <caption className="sr-only">
            Every executed trade, with the session whose close produced the signal
          </caption>
          <thead>
            <tr className="border-b border-line text-left text-label text-faint">
              <th className="py-2 pr-3 font-medium">Executed</th>
              <th className="py-2 pr-3 font-medium">Signalled</th>
              <th className="py-2 pr-3 font-medium">Side</th>
              <th className="py-2 pr-3 font-medium">Quantity</th>
              <th className="py-2 pr-3 font-medium">Execution price</th>
              <th className="py-2 pr-3 font-medium">Value</th>
              <th className="py-2 pr-3 font-medium">Commission</th>
              <th className="py-2 font-medium">Cash after</th>
            </tr>
          </thead>
          <tbody>
            {run.trades.map((trade) => (
              <tr
                key={`${trade.executed_on}-${trade.side}`}
                className="border-b border-line/60"
              >
                <td className="py-2 pr-3 text-ink">{trade.executed_on}</td>
                {/* Always exactly one session earlier than the execution -- that is the rule,
                    and showing both is what makes it checkable rather than merely stated. */}
                <td className="py-2 pr-3 text-faint">{trade.signalled_on}</td>
                <td
                  className={`py-2 pr-3 font-medium ${
                    trade.side === 'buy' ? 'text-positive' : 'text-danger'
                  }`}
                >
                  {trade.side}
                </td>
                <td className="py-2 pr-3 tabular-nums text-dim">
                  {formatShares(String(trade.quantity))}
                </td>
                <td className="py-2 pr-3 tabular-nums text-dim">
                  {formatCurrency(trade.price)}
                </td>
                <td className="py-2 pr-3 tabular-nums text-dim">
                  {formatCurrency(trade.gross_value)}
                </td>
                <td className="py-2 pr-3 tabular-nums text-dim">
                  {formatCurrency(trade.commission)}
                </td>
                <td className="py-2 tabular-nums text-dim">{formatCurrency(trade.cash_after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// --- the explanation --------------------------------------------------------------------------

/** What asking for an explanation is doing, which is its own state and not the run's.
 *
 * Kept apart from `RunState` on purpose: an explanation that cannot be written leaves the
 * results exactly as they were, so the two must be able to fail independently.
 */
type ExplainState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'ready'; text: string; model: string | null; promptVersion: string | null }
  | { phase: 'unavailable'; detail: string; retryable: boolean }

/** The explanation of one run: readable prose, or the reason there is not any.
 *
 * **The results do not depend on this.** Everything above it was calculated in Python and is on
 * screen before this is asked for, so a model that is not configured, does not answer or writes
 * something the server refuses leaves the figures untouched and says so here. That is why the
 * button and the retry live in this panel rather than replacing the page.
 *
 * Written by a model from the run's own figures and stored with it, so a reload shows the same
 * words rather than asking again -- and the panel says who wrote them, because prose about a
 * performance figure is not itself a measurement.
 */
function ExplainPanel({ run }: { run: BacktestRun }) {
  const [state, setState] = useState<ExplainState>(() =>
    run.explanation === null
      ? { phase: 'idle' }
      : {
          phase: 'ready',
          text: run.explanation,
          model: run.explanation_model,
          promptVersion: run.explanation_prompt_version,
        },
  )

  const ask = useCallback(async () => {
    setState({ phase: 'loading' })
    try {
      const body = await explainBacktestRun(run.run_id)
      if (body.status === 'ok' && body.explanation !== null) {
        setState({
          phase: 'ready',
          text: body.explanation,
          model: body.model,
          promptVersion: body.prompt_version,
        })
        return
      }
      setState({
        phase: 'unavailable',
        detail: body.detail ?? 'The explanation could not be written.',
        // A missing key is not worth offering again; a provider that did not answer is.
        retryable: body.reason !== 'model_not_configured',
      })
    } catch (error) {
      setState({ phase: 'unavailable', detail: messageOf(error), retryable: true })
    }
  }, [run.run_id])

  return (
    <div className="border-t border-line pt-4">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h3 className="text-label font-medium text-dim">Explanation</h3>
        {state.phase === 'idle' && (
          <Button onClick={() => void ask()}>Explain results</Button>
        )}
      </div>

      {state.phase === 'idle' && (
        <p className="mt-2 text-note text-dim">
          A written explanation of these figures, from the run&rsquo;s own numbers. It is prose,
          not a calculation, and it changes nothing on this page.
        </p>
      )}

      {state.phase === 'loading' && (
        <p className="mt-2 text-note text-dim" role="status">
          Asking the model to explain these figures&hellip;
        </p>
      )}

      {state.phase === 'ready' && (
        <>
          <div className="mt-2 space-y-3 text-body text-ink">
            {state.text.split('\n\n').map((paragraph, index) => (
              <p key={index}>{paragraph}</p>
            ))}
          </div>
          <p className="mt-3 text-label text-faint">
            Written by a model ({state.model ?? 'unknown'}, prompt version{' '}
            {state.promptVersion ?? 'unknown'}) from this run&rsquo;s recorded figures. It is not
            a calculation, not evidence, and not a forecast.
          </p>
        </>
      )}

      {state.phase === 'unavailable' && (
        <div className="mt-2">
          <p className="text-note text-dim">
            The explanation could not be written. {state.detail}
          </p>
          <p className="mt-1 text-label text-faint">
            The results above are unaffected &mdash; they were calculated and stored with the run.
          </p>
          {state.retryable && (
            <Button className="mt-2" onClick={() => void ask()}>
              Try again
            </Button>
          )}
        </div>
      )}
    </div>
  )
}

function Disclosures({ run, strategyName }: { run: BacktestRun; strategyName: string | null }) {
  return (
    <details className="border-t border-line pt-4">
      <summary className="cursor-pointer text-label font-medium text-dim">
        How this was calculated, and what it does not tell you
      </summary>

      <div className="mt-3 space-y-3 text-note text-dim">
        <div>
          <p className="text-label text-faint">Strategy</p>
          <p className="mt-0.5 text-ink">
            {strategyName ?? run.strategy_name} (version {run.strategy_version})
          </p>
        </div>

        <div>
          <p className="text-label text-faint">Costs and execution</p>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            {run.assumptions.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>

        <div>
          <p className="text-label text-faint">Data</p>
          <p className="mt-1">
            Daily bars from {run.provider}, {run.adjustment_basis} for{' '}
            {run.provider_adjust_mode}, read {run.data_retrieved_at.slice(0, 10)}. The test
            covered {run.coverage?.sessions ?? 0} sessions from{' '}
            {run.coverage?.first_session ?? PLACEHOLDER} to{' '}
            {run.coverage?.last_session ?? PLACEHOLDER}
            {run.coverage != null &&
              `, on top of ${run.coverage.warmup_sessions} sessions of history fetched only so the averages had a value when the test opened`}
            .
          </p>
        </div>

        <div>
          <p className="text-label text-faint">What this is not</p>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            {run.limitations.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      </div>
    </details>
  )
}
