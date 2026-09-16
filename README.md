# AlphaDesk AI

Autonomous quant and investment analytics platform. A FastAPI backend and PostgreSQL,
running under Docker Compose, storing portfolios and their holdings.

## What exists today

Two services:

- **backend** — FastAPI on port 8000, hot-reloading.
- **postgres** — PostgreSQL 18, with a named volume so data survives restarts.

Three endpoints:

| Endpoint | Purpose | Behaviour when the database is down |
|---|---|---|
| `GET /health` | Liveness — is the API process up? | Still `200` |
| `GET /health/db` | Readiness — can the API reach PostgreSQL? | `503` |
| `GET /portfolio` | The seeded demo portfolio and its holdings | `503` |

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
runs inside the container.

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
takes a few seconds longer than the backend itself needs.

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

Interactive API docs: <http://localhost:8000/docs> — `GET /portfolio` can be run from there
with the "Try it out" button.

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

## Logs

```bash
docker compose logs -f backend     # application logs, including database failures
docker compose logs -f postgres    # database logs
docker compose logs -f             # both
```

## Shutdown

```bash
docker compose down          # stop and remove containers; data is KEPT
docker compose down -v       # stop and remove containers AND DELETE ALL DATA
```

Avoid `-v` unless you genuinely want to wipe the database.

## Editing code

`./backend` is bind-mounted into the container and uvicorn runs with `--reload`, so saving a
`.py` file restarts the app within about a second. No rebuild, no restart command.

Rebuild only when `requirements.txt` or the `Dockerfile` changes:

```bash
docker compose up --build -d
```

Dependencies are installed in a separate image layer and pip's download cache is kept as a
BuildKit cache mount, so a rebuild after a code change is fast.

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

Nothing is computed from the stored values. Market value, profit/loss, and exposure are all
absent because they need current prices, which do not exist yet.

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

- **The demo holdings are synthetic.** The `average_buy_price` values (NVDA 120.00,
  AAPL 180.00, MSFT 400.00) are fictional purchase prices recorded on the demo portfolio.
  They are not current market prices and nothing in the API treats them as such.
- **Pinned versions are not a full reproducibility guarantee.** `requirements.txt` pins the
  five direct dependencies exactly, but their transitive dependencies (starlette, pydantic,
  and so on) resolve to whatever satisfies their ranges at build time. Two builds months
  apart can therefore differ. Locking those down needs a tool like `pip-compile` or `uv`.
- **`postgres:18-alpine` stores data at `/var/lib/postgresql`, not the widely-documented
  `/var/lib/postgresql/data`.** Postgres 18 changed its layout. If you change the image tag,
  check the volume target — mounting the wrong path silently fails to persist anything.
- **The bind mount means development runs your source files, not the copy inside the image.**
  A broken `COPY app ./app` in the Dockerfile would still appear to work locally.
