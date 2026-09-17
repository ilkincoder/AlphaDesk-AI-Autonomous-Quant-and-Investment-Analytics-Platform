# AlphaDesk AI

Autonomous quant and investment analytics platform. A React frontend, a FastAPI backend,
and PostgreSQL, running under Docker Compose.

## What exists today

Three services:

- **frontend** — React + Vite on port 5173, hot-reloading. The dashboard.
- **backend** — FastAPI on port 8000, hot-reloading.
- **postgres** — PostgreSQL 18, with a named volume so data survives restarts.

**Open the app at <http://localhost:5173>.** Two screens work:

- **Portfolio** — the overview, with the valuation figures from `GET /portfolio/valuation`.
- **What if?** — pick a holding, enter a percentage move between −100% and +100%, and see
  the hypothetical before and after. The stock list and every figure come from the backend.

The remaining eight navigation items and four of the five Portfolio tabs are visibly
disabled — they lead nowhere yet, on purpose.

Five API endpoints:

| Endpoint | Purpose | Behaviour when the database is down |
|---|---|---|
| `GET /health` | Liveness — is the API process up? | Still `200` |
| `GET /health/db` | Readiness — can the API reach PostgreSQL? | `503` |
| `GET /portfolio` | The seeded demo portfolio and its holdings | `503` |
| `GET /portfolio/valuation` | The same portfolio valued at the demo prices | `503` |
| `POST /portfolio/scenario` | One holding re-priced, and what it does to the total | `503` |

Liveness and readiness are separate on purpose: a process that is running but cannot
reach its database should not report itself as healthy.

### Database schema

Two tables, created by an Alembic migration:

**`portfolios`**

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` | primary key |
| `name` | `varchar(100)` | required, unique |
| `cash_balance` | `numeric(18,2)` | required, `>= 0` |
| `currency` | `varchar(3)` | required, `USD` for the demo |

**`holdings`**

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` | primary key |
| `portfolio_id` | `integer` | required, FK to `portfolios`, `ON DELETE CASCADE` |
| `symbol` | `varchar(20)` | required |
| `quantity` | `numeric(18,6)` | required, `> 0` |
| `average_buy_price` | `numeric(18,4)` | required, `> 0` |

Unique on `(portfolio_id, symbol)`. Long positions only — there is no way to represent
a short.

Money is `NUMERIC`, never `FLOAT`. Binary floating point cannot represent values like
`0.10` exactly, so summing cash in floats drifts by tiny amounts. `NUMERIC` stores exact
decimal digits and maps to Python `Decimal`. The differing scales are deliberate:
cash is genuinely counted in cents, quantity leaves room for fractional shares, and an
*average* price can have more than two decimals (100 ÷ 3), so rounding it to cents would
quietly lose precision.

## Prerequisites

Docker Desktop, with Compose v2 or later. Nothing needs to be installed locally — Python
and Node both run inside their containers.

## Setup

```bash
cp .env.example .env
```

The values in `.env.example` are **for local development only**. They are not secrets and
must not be reused anywhere reachable from an untrusted network. `.env` is gitignored.

## Start

```bash
docker compose up --build -d
```

The backend waits for the Postgres healthcheck to pass before starting, so the first start
takes a few seconds longer than the backend itself needs. The frontend is the quickest of
the three; Vite is ready in well under a second.

Open <http://localhost:5173>. If the page loads but the numbers do not, the backend is the
thing to check — see "When the API is unreachable" below.

## Frontend

React 19, Vite 8, TypeScript and Tailwind CSS 4, in `frontend/`. It is a **development**
server, not a production build: the container exists to give you hot reload.

```bash
docker compose up -d frontend          # just this service
docker compose logs -f frontend        # the Vite output, including proxy errors
docker compose exec frontend npm test  # unit tests
docker compose exec frontend npm run typecheck
docker compose exec frontend npm run build
```

### How the frontend reaches the API

The browser calls **relative** URLs — `/api/portfolio/valuation` — never
`http://backend:8000/...`. Vite forwards them:

```
browser                Vite dev server              FastAPI              Postgres
────────               ───────────────              ───────              ────────
GET /api/portfolio/valuation
        │
        └──► 5173 (same origin)
                     │
                     │ matches the '/api' proxy rule
                     │ strips the /api prefix
                     └──► http://backend:8000/portfolio/valuation
                                        │
                                        └──► SELECT ... FROM portfolios / holdings
```

Configured in `frontend/vite.config.ts`. Three things follow from it:

- **No CORS.** The browser only ever talks to `localhost:5173`, its own origin, so there is
  no cross-origin request for the backend to allow. If you point the browser straight at
  port 8000 instead, you will need CORS — which is why you should not.
- **The `/api` prefix exists only as a boundary marker.** FastAPI never sees it; the proxy
  strips it before forwarding.
- **`backend` is resolved by Docker, not by your Mac.** The proxy runs *inside* the
  frontend container, where `backend` is a Compose service name. That hostname means
  nothing to your browser, which is exactly why the browser is not the one using it.

Because the proxying happens server-side, nothing about the backend's address reaches the
client, and the frontend container is given no `environment:` block at all — it never sees
a database credential.

### When the API is unreachable

The page says so rather than showing anything invented:

| What happened | What you see |
|---|---|
| The portfolio has not been seeded | "No portfolio available." plus the seed command |
| The backend is down | "Could not load the portfolio." with the proxy's error |
| A refresh fails while numbers are on screen | The previous numbers, labelled "Showing previously loaded values" |
| The portfolio has no holdings | "No stock holdings yet", with cash and totals still shown |
| Total value is zero | "Allocation unavailable for a zero-value portfolio." |

Nothing on this page ever displays P&L, returns, or a daily change. Those need data that
does not exist yet, and a placeholder number would be worse than an empty space.

## Set up the database

Two separate steps. The tables are **not** created automatically when the API starts —
schema changes are explicit, and seeding is a decision you make, not something that happens
silently on every boot.

**1. Apply migrations** (creates or updates the tables):

```bash
docker compose exec backend alembic upgrade head
```

**2. Seed the demo portfolio** (inserts the data):

```bash
docker compose exec backend python -m app.seed
```

You only need the seed once. Running it again is safe: it inserts anything missing and
leaves everything else **exactly as it is**, so a cash balance or holding you have since
changed is never reset.

Expected seed output on a first run:

```
created portfolio 'AlphaDesk Demo'
  NVDA: added
  AAPL: added
  MSFT: added
```

## Verify

`-w` prints the HTTP status code and elapsed time after the body, so you can check the
`200`/`503` behaviour directly:

```bash
curl -s -w '\nHTTP %{http_code} in %{time_total}s\n' localhost:8000/health
# {"status":"ok"}
# HTTP 200 in 0.00s

curl -s -w '\nHTTP %{http_code} in %{time_total}s\n' localhost:8000/health/db
# {"status":"ok","database":"connected"}
# HTTP 200 in 0.01s

curl -s -o /dev/null -w 'HTTP %{http_code}\n' localhost:8000/docs
# HTTP 200
```

The demo portfolio (404 until you have seeded it):

```bash
curl -s -w '\nHTTP %{http_code}\n' localhost:8000/portfolio
# {"id":1,"name":"AlphaDesk Demo","currency":"USD","cash_balance":"10000.00",
#  "holdings":[{"symbol":"AAPL","quantity":"5.000000","average_buy_price":"180.0000"},
#              {"symbol":"MSFT","quantity":"8.000000","average_buy_price":"400.0000"},
#              {"symbol":"NVDA","quantity":"10.000000","average_buy_price":"120.0000"}]}
# HTTP 200
```

Money and quantities come back as **strings**, not JSON numbers, and holdings are always
sorted by symbol. See "Why decimals are strings" below.

The same portfolio, valued at the demo prices:

```bash
curl -s -w '\nHTTP %{http_code}\n' localhost:8000/portfolio/valuation
# {"portfolio_id":1,"currency":"USD","price_source":"demo","cash_balance":"10000.00",
#  "holdings_value":"6100.00","total_value":"16100.00","cash_allocation_percent":"62.11",
#  "holdings":[
#    {"symbol":"AAPL","quantity":"5.000000","price":"200.00","holding_value":"1000.00","allocation_percent":"6.21"},
#    {"symbol":"MSFT","quantity":"8.000000","price":"450.00","holding_value":"3600.00","allocation_percent":"22.36"},
#    {"symbol":"NVDA","quantity":"10.000000","price":"150.00","holding_value":"1500.00","allocation_percent":"9.32"}]}
# HTTP 200
```

### Demo prices

Valuation needs a price per holding, and there is no market-data feed yet, so it uses one
small hardcoded table in `backend/app/valuation.py`:

| Symbol | Demo price |
|---|---|
| `AAPL` | 200.00 |
| `MSFT` | 450.00 |
| `NVDA` | 150.00 |

These are **fictional valuation prices**. They are not live quotes, and they are not the
`average_buy_price` stored on the holdings — a purchase price answers a different question
and would produce a meaningless "valuation". The response says `"price_source": "demo"` so
nothing downstream can mistake them for market data, and it reports no timestamp, because
there is no market event to attach a time to.

### The formulas

```
holding_value           = quantity × price
holdings_value          = sum of holding values
total_value             = holdings_value + cash_balance
allocation_percent      = holding_value / total_value × 100
cash_allocation_percent = cash_balance  / total_value × 100
```

Every figure is computed in `Decimal` at full precision and rounded only on the way out —
to two decimal places, with `ROUND_HALF_UP` (halves away from zero, not Python's default
half-to-even). Two consequences are worth knowing, and neither is a bug:

- **The holdings may not sum to `holdings_value` to the cent.** That total is rounded once
  from the unrounded values, while the line items are rounded individually. Rounding every
  intermediate step would accumulate error instead of avoiding it.
- **The percentages may not sum to exactly 100.** Three equal holdings each show `33.33`,
  which totals `99.99`.

Behaviour:

- **Missing portfolio** → `404`.
- **No price for a held symbol** → `503`, naming the symbol(s). Deliberately not a total
  that quietly omits a holding.
- **Portfolio with no holdings** → `holdings_value` is `0.00`, `total_value` is the cash.
- **Zero total value** → `cash_allocation_percent` and every `allocation_percent` are
  `null`. With nothing to divide by, the share is undefined rather than zero.
- **Read-only.** It runs a `SELECT` and multiplies. Nothing is written.

### Scenario: what if one holding moves?

```bash
curl -s -X POST localhost:8000/portfolio/scenario \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"NVDA","price_change_percent":"-10"}'
# {"portfolio_id":1,"currency":"USD","price_source":"demo","symbol":"NVDA",
#  "price_change_percent":"-10",
#  "price_before":"150.00","price_after":"135.00",
#  "holding_value_before":"1500.00","holding_value_after":"1350.00",
#  "total_value_before":"16100.00","total_value_after":"15950.00",
#  "change_value":"-150.00","change_percent":"-0.93"}
```

**One price moves; nothing else does.** Cash, the other holdings, and every quantity are
identical on both sides, which is what makes the difference attributable to the single
move the request named. `change_percent` is against the *before* total.

`price_change_percent` is a signed percentage, and it is sent as a **string** — `Decimal`
parses `"-10"` exactly, whereas a JSON number would have gone through a float first. The
value is bounded to −100 (the whole price) and +100.

`POST` rather than `GET` because the question has a body rather than a path: *what if this
symbol moved by this much*. Nothing is written — the same rows are read, arithmetic is
applied, and the answer is returned.

| Situation | Response |
|---|---|
| Stock moves, everything fine | `200` |
| Portfolio not seeded | `404` |
| `symbol` is not held by the portfolio | `404`, listing what is held |
| `price_change_percent` outside −100..100, or `symbol` empty | `422` |
| A held symbol has no demo price | `503` |

Interactive API docs: <http://localhost:8000/docs> — every endpoint, including the POST,
can be run from there with the "Try it out" button.

To confirm the two endpoints really are independent, stop the database and check both:

```bash
docker compose stop postgres

curl -s -w '\nHTTP %{http_code}\n' localhost:8000/health      # still 200
curl -s -w '\nHTTP %{http_code}\n' localhost:8000/health/db   # 503

docker compose start postgres
curl -s -w '\nHTTP %{http_code}\n' localhost:8000/health/db   # 200 again, no backend restart
```

The backend recovers on its own because the engine uses `pool_pre_ping`, which discards
connections that died while Postgres was away.

### Tests

`backend/tests/` covers the valuation calculation and the endpoint's error paths, using
the standard library's `unittest` — no extra dependency to install:

```bash
docker compose exec backend python -m unittest discover -s tests -t .
# Ran 40 tests in 0.005s
# OK
```

`test_valuation.py` and `test_scenario.py` are pure arithmetic and need no database or
running service at all. The two endpoint files import `app.main` (so `DATABASE_URL` must
be set, which is why they run in the container) but use a stub session, so they open no
connection and cannot alter the demo portfolio. The `-100..+100` bound is validated by
Pydantic before the handler runs, so it is checked against the live server with the `curl`
commands above rather than from a handler test. The endpoint is also worth checking by hand with the
`curl` commands above, since those read the real seeded rows.

`frontend/src/**/*.test.ts(x)` covers the display formatting and the page's states, with
`fetch` stubbed. Every failure, empty, and zero-value case is a fixture — none of them
edits the seeded portfolio to provoke a state:

```bash
docker compose exec frontend npm test
# Test Files  5 passed (5)
#      Tests  61 passed (61)
```

## Logs

```bash
docker compose logs -f frontend    # Vite, including proxy errors
docker compose logs -f backend     # application logs, including database failures
docker compose logs -f postgres    # database logs
docker compose logs -f             # all three
```

## Shutdown

```bash
docker compose down          # stop and remove containers; data is KEPT
docker compose down -v       # stop and remove containers AND DELETE ALL DATA
```

Avoid `-v` unless you genuinely want to wipe the database.

## Editing code

Both source directories are bind-mounted and both servers watch them.

- Saving a `.py` file under `./backend` restarts uvicorn within about a second.
- Saving a `.tsx`/`.ts`/`.css` file under `./frontend` hot-reloads the page — usually
  without losing component state.

No rebuild, no restart command for either.

Rebuild only when a manifest or Dockerfile changes — `backend/requirements.txt`,
`backend/Dockerfile`, `frontend/package.json`, or `frontend/Dockerfile`:

```bash
docker compose up --build -d
```

Dependencies are installed in their own image layers, and both pip's and npm's download
caches are kept as BuildKit cache mounts, so a rebuild after a code change is fast.

### Node dependencies

`frontend/node_modules` in the container is an **anonymous volume**, so it is not the same
directory as `frontend/node_modules` on your Mac — which normally does not exist at all.
The image installs its own Linux dependencies with `npm ci`; the mount stops a host
`npm install` from shadowing them with macOS binaries.

To add a package, edit `frontend/package.json`, then:

```bash
npm --prefix frontend install --package-lock-only   # updates the lockfile, no host node_modules
docker compose build frontend
docker compose up -d frontend
```

## Connecting from your Mac

The container publishes Postgres on **port 5433**, not 5432, because a native PostgreSQL
already listens on 5432 on this machine. From Mac-side tools (DataGrip / DB Navigator, psql):

| Setting | Value |
|---|---|
| Host | `localhost` |
| Port | `5433` |
| Database | `alphadesk` |
| User | `alphadesk` |
| Password | `alphadesk_dev_password` |

Inside the Compose network the same database is at host `postgres`, port `5432` — see
"Why the hostname differs" below.

## Migrations vs seeds

They are different jobs and are deliberately kept apart.

| | Migration | Seed |
|---|---|---|
| Changes | **structure** — tables, columns, constraints | **data** — rows |
| Tool | Alembic | `python -m app.seed` |
| Versioned | Yes — each one is recorded in `alembic_version` | No |
| Run | When the schema needs to change | When you need the demo data |
| Re-runnable | Each migration runs **once**, then is recorded as applied | Yes, safely, as often as you like |

A migration is a change to the *shape* of the database. Every migration has a revision ID and
a `down_revision`, forming a chain; Alembic records which ones have run in an `alembic_version`
table and applies only the ones still outstanding. So `alembic upgrade head` on a fresh
database builds the whole schema, and on an up-to-date one does nothing. Migrations are
one-way-once by design: you do not want "create table" running twice.

A seed is a change to the *contents*. It has no version and no history — it just makes the
data match what you asked for. That is why it must be idempotent (safe to repeat) while a
migration must not be.

Keeping them separate means the schema can be applied to any environment without dragging
demo rows along, and demo data can be reloaded without touching the schema.

## Why decimals are strings

`GET /portfolio` returns `"10000.00"` as a JSON **string**, not the number `10000.00`.

JSON numbers are IEEE-754 doubles. A client that parsed `10000.00` into a float would
reintroduce exactly the precision problem the `NUMERIC` columns exist to avoid — the value
would be fine for small numbers and quietly wrong once arithmetic accumulated. Sending a
string keeps it exact and lets the client choose how to handle it.

The trailing zeros come from the column's scale, so the format is deterministic: `quantity`
always shows 6 decimal places, `average_buy_price` 4, `cash_balance` 2. Pydantic does this by
default for `Decimal` fields, so there is no custom serialiser to go looking for.

## How it fits together

**A request's path.** `curl localhost:8000/health/db` connects to a port published by Docker
on the host. Docker forwards it to port 8000 inside the backend container, where uvicorn is
listening. Uvicorn matches the path against the routes FastAPI registered and calls
`health_db()`. That function borrows a connection from SQLAlchemy's connection pool, runs
`SELECT 1`, returns it to the pool, and returns a dict that FastAPI serialises to JSON.

**Reading a portfolio.** `GET /portfolio` follows the same path, then does more:

1. FastAPI sees the handler takes a `Session` and calls the `get_session` dependency first.
   That opens a session (one per request) and hands it to the handler. The dependency is
   written with `with SessionLocal() as session:`, so the session is closed on the way out
   even if the handler raises.
2. The handler asks for the `Portfolio` whose `name` is `"AlphaDesk Demo"`. SQLAlchemy turns
   that into `SELECT ... FROM portfolios WHERE name = %s`, borrowing a pooled connection.
3. `selectinload(Portfolio.holdings)` makes SQLAlchemy immediately issue a second query for
   that portfolio's holdings rather than deferring it. Without it the holdings would load
   *lazily*, at the moment they are read — which may be after the session has closed, giving
   `DetachedInstanceError`. The relationship carries `order_by="Holding.symbol"`, so the
   second query sorts by symbol and the JSON order is stable.
4. If no row came back, the handler raises `HTTPException(404, ...)` and FastAPI renders
   `{"detail": "..."}`.
5. Otherwise `PortfolioOut.model_validate(portfolio)` reads attributes off the ORM objects
   and builds the response model, converting each `Decimal` to a string on the way out.

`GET /portfolio` computes nothing from the stored values — it is a straight read.

**Valuing a portfolio.** `GET /portfolio/valuation` does the same read, then hands the
result to `calculate_valuation()` in `app/valuation.py`. That function takes holdings, a
cash balance, and a price mapping, and returns a dataclass of `Decimal`s. It imports no
database and no HTTP code, and writes nothing, so it can be unit-tested with plain values
and reused later by something that is not an HTTP handler at all. The handler's only jobs
are to fetch the rows, map a missing price to `503`, and copy the result into the response
schema.

Profit/loss and exposure are still absent: they need purchase dates, sale history, or
benchmarks, none of which exist yet.

**Showing it on a page.** `GET /portfolio/valuation` is the only endpoint the frontend
calls. `PortfolioPage` asks for it through `api.ts`, which requests the relative path
`/api/portfolio/valuation`; Vite's proxy (running inside the frontend container) rewrites
that to `http://backend:8000/portfolio/valuation` and the request completes as above. The
JSON that comes back is stored as-is and handed to two presentational components.

No figure is recalculated in the browser. Every amount arrives as a decimal string and is
only *punctuated* for display — `format.ts` inserts thousands separators with a regular
expression over the digits rather than calling `Number()`, because parsing to a float is
exactly the precision loss the `NUMERIC` columns and the string serialisation exist to
avoid. The allocation bar's segment widths are the API's percentage strings used directly
as CSS widths, which is also why a portfolio whose rounded percentages do not total 100
still renders honestly.

**How the backend reaches Postgres.** The connection pool creates its connections from a
single `DATABASE_URL`. On the first `engine.connect()` the pool opens a real TCP connection
to whatever host that URL names, authenticates, and keeps it for reuse. `pool_pre_ping`
re-tests a pooled connection before handing it out; `connect_timeout` caps how long a new
connection attempt may take.

**Why the hostname differs.** `postgres` and `localhost` are two different addresses because
they are resolved by two different computers:

- The backend container resolves `postgres` using Docker's internal DNS, which maps each
  Compose service name to that container's IP on the private Compose network. That network is
  invisible from your Mac, and `localhost` *inside* the container would mean the container
  itself — which is not where the database is.
- Your Mac resolves `localhost` to itself and reaches the database through the published port
  `5433`. It has no idea the name `postgres` exists.

That is why `docker-compose.yml` builds the container's `DATABASE_URL` with host `postgres`
while your Mac-side tools use `localhost:5433`. It is the same database either way.

## Notes and limitations

- **The frontend runs Vite's development server, not a production build.** That is
  deliberate — it is what gives hot reload — but it means there is no nginx, no static
  file serving, no minification, and no cache headers. Putting this online needs a build
  step and a real web server, plus a decision about where the `/api` proxy lives.
- **No router, and therefore no URLs.** Which page is showing is React state in `App.tsx`,
  not a route. Two destinations did not justify the dependency or the rework. The costs are
  real though: a refresh always returns to Portfolio, the What if? page cannot be linked to
  or bookmarked, and the browser's Back button does not move between them. Adding
  `react-router` later is a contained change — `PageId` in `components/Sidebar.tsx`
  already names the destinations, and the nav list is the single place they are declared.
- **The scenario calculation only moves one price at a time.** Correlated moves, a shock
  applied to the whole portfolio, and a fall below −100% are all out of scope. A price
  cannot go negative, and `calculate_scenario` refuses a fraction below `-1` rather than
  producing one.
- **`calculate_scenario` is pure, and stays that way.** Holdings, cash, prices, a symbol,
  and a *fraction* in; numbers out. It imports no database and no HTTP, and takes a
  fraction rather than a percentage so that parsing "-10" stays the caller's business —
  which is what lets an endpoint and, later, an AI tool call the same function with their
  own phrasing. It is built by calling `calculate_valuation` twice, so the "before" column
  can never drift from what `GET /portfolio/valuation` reports.
- **The frontend is USD-only in practice.** It renders a `$` symbol and the sidebar shows
  "USD" as a static shell label, while the API's `currency` field is carried but only used
  for display. A non-USD portfolio would be mislabelled.
- **The demo holdings are synthetic.** The `average_buy_price` values (NVDA 120.00,
  AAPL 180.00, MSFT 400.00) are fictional purchase prices recorded on the demo portfolio.
  They are not current market prices and nothing in the API treats them as such.
- **The demo valuation prices are synthetic too.** AAPL 200.00, MSFT 450.00, NVDA 150.00
  in `backend/app/valuation.py` are invented. Every number `GET /portfolio/valuation`
  returns is derived from them, so the output is only ever as real as that table. Swapping
  in a real price source means replacing that mapping, not changing the arithmetic.
- **Pinned versions are not a full reproducibility guarantee.** `requirements.txt` pins the
  five direct dependencies exactly, but their transitive dependencies (starlette, pydantic,
  and so on) resolve to whatever satisfies their ranges at build time. Two builds months
  apart can therefore differ. Locking those down needs a tool like `pip-compile` or `uv`.
- **`postgres:18-alpine` stores data at `/var/lib/postgresql`, not the widely-documented
  `/var/lib/postgresql/data`.** Postgres 18 changed its layout. If you change the image tag,
  check the volume target — mounting the wrong path silently fails to persist anything.
- **The bind mount means development runs your source files, not the copy inside the image.**
  A broken `COPY app ./app` in the Dockerfile would still appear to work locally. The same
  applies to `tests/`, which the Dockerfile does not copy in at all — the test command
  above works only because `./backend` is mounted over `/app`.
