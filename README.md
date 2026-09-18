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

None of that changed as the schema grew, and none of it will: the tables behind Module 1
have no endpoint, and nothing a command writes is served on this page.

Eight commands have since arrived, none of them a route. `python -m app.fetch_prices` fetches
Twelve Data daily bars, `python -m app.fetch_sec` fetches SEC Form 4 filings, and
`python -m app.ingest_nvda` fetches both and **stores** them — `python -m app.ingest_company`
does the same for any issuer named on the command line, and is the command that implements
both. `python -m app.analyze_insiders` reads that back and describes it.
`python -m app.ingest_company_context` stores a bounded set of disclosures — a 10-K, a 10-Q,
recent 8-Ks, their earnings exhibits and selected financial facts — with the readable text of
each. `python -m app.index_filings` embeds that text into Qdrant, and
`python -m app.search_filings` retrieves passages from it. Every endpoint on this page behaves
exactly as it did before, and none of them reads any of it yet.

### Database schema

Eleven tables, created by six Alembic migrations. The two here came first and back the
portfolio endpoints. The rest belong to Module 1: five in **Market data and SEC filings**,
one (`ingestion_runs`) with ingestion, and three in **Company disclosures** below. None of
them is read by an endpoint yet.

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

### Market data and SEC filings

Five more tables, added by the `0002_market_data_and_filings` migration. **No endpoint
reads them yet** — this milestone is storage only, so that Module 1 has somewhere to put
prices and filings before anything consumes them.

**`companies`** — a US-listed company, one primary listing each.

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` | primary key |
| `name` | `varchar(255)` | required |
| `ticker` | `varchar(20)` | required, unique |
| `exchange` | `varchar(32)` | required |
| `currency` | `varchar(3)` | required |
| `sec_issuer_cik` | `text` | required, unique, digits only |

`sec_issuer_cik` is `text`, not an integer, because CIKs carry leading zeros: Apple is
`0000320193`, and an integer column would store `320193`. It is also the **issuer** CIK.
A reporting owner's CIK is a different number about a different entity, and lives on
`insider_reporting_owners`.

A company is deliberately unrelated to `holdings`. There is no foreign key between them:
a company is researched whether or not anyone holds it, and matching a holding to a
company is a query someone writes, not a constraint the database enforces.

**`daily_prices`** — one trading session, as one provider reported it.

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` | primary key |
| `company_id` | `integer` | required, FK to `companies`, `ON DELETE CASCADE` |
| `trading_date` | `date` | required |
| `open` `high` `low` `close` | `numeric` | required. Unconstrained since `0003`, so the provider's own precision is stored |
| `volume` | `bigint` | required, `>= 0` |
| `provider` | `varchar(32)` | required |
| `currency` | `varchar(3)` | required |
| `adjustment_basis` | `varchar(16)` | required, `raw` or `adjusted` |
| `retrieved_at` | `timestamptz` | required, defaults to `now()` |

Unique on `(company_id, trading_date, provider, adjustment_basis)` — one bar per company
per day per provider per basis. Raw and adjusted prices are different numbers describing
the same day, so they are kept apart rather than allowed to overwrite each other.

Checks: `low > 0`, `high >= low`, and both `open` and `close` inside the `low`–`high`
range. Together these reject any incoherent bar, and they also imply that all four prices
are positive — so there are deliberately no separate `> 0` checks on open, high and close.
Volume counts shares rather than money, so it is a `bigint`, not `NUMERIC`.

`provider` is free-form on purpose: adding a second provider should not need a migration.

**`sec_filings`** — one EDGAR filing, and the identity its transactions hang from.

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` | primary key |
| `company_id` | `integer` | required, FK to `companies`, `ON DELETE CASCADE` |
| `accession_number` | `varchar(25)` | required, unique |
| `form_type` | `varchar(16)` | required, e.g. `4`, `4/A` |
| `filing_date` | `date` | required |
| `acceptance_datetime` | `timestamptz` | **nullable** |
| `source_document_url` | `varchar(512)` | required |
| `retrieved_at` | `timestamptz` | required, defaults to `now()` |
| `is_amendment` | `boolean` | required, defaults to `false` |
| `amends_filing_id` | `integer` | nullable, FK to `sec_filings`, `ON DELETE SET NULL` |

**Three different times, kept apart on purpose:**

- `filing_date` — the SEC's own calendar date for the filing.
- `acceptance_datetime` — the moment EDGAR accepted it, when the source supplies one.
- `retrieved_at` — when *we* fetched it.

None of them is the `transaction_date` below, and none is the moment the market learned
anything. Storing one and inferring the others is exactly what makes "what did the public
know, and when" unanswerable later.

`is_amendment` and `amends_filing_id` are stored but **never populated automatically** in
this milestone. Deciding that one accession number amends another is a judgement for a
later step, and a guess here would silently double-count transactions.

Indexed on `(company_id, filing_date)`.

**`insider_transactions`** — one reported transaction row from a Form 4.

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` | primary key |
| `filing_id` | `integer` | required, FK to `sec_filings`, `ON DELETE CASCADE` |
| `source_table` | `varchar(24)` | required, `nonDerivativeTable` or `derivativeTable` |
| `row_position` | `integer` | required, `>= 0` |
| `is_derivative` | `boolean` | required |
| `transaction_date` | `date` | required |
| `security_title` | `varchar(255)` | required |
| `transaction_code` | `varchar(4)` | required, the raw EDGAR code |
| `acquired_disposed` | `varchar(1)` | required, `A` or `D` |
| `shares` | `numeric` | required, `> 0`. Unconstrained since `0003` |
| `price_per_share` | `numeric` | **nullable**. Unconstrained since `0003` |
| `ownership_direct_indirect` | `varchar(1)` | required, `D` or `I` |
| `footnotes` | `text` | nullable |

Unique on `(filing_id, source_table, row_position)`. A transaction's identity is *where
it came from* — this row, of this table, of this filing — never its date and amount,
because two genuinely separate transactions can share both. `row_position` is the 0-based
index of the transaction element within its source table, in document order, which is
stable for a given accession number.

`transaction_code` is stored exactly as EDGAR reports it and is **deliberately not
constrained to a list**. Grants (`A`), gifts (`G`), option exercises (`M`) and tax
withholding (`F`) have to stay distinguishable, and a closed list would reject a code
EDGAR introduces later.

`price_per_share` is nullable, and its check is `>= 0` rather than `> 0`. A price reported
as zero is real data and is stored; a price that is missing stays `NULL`. Defaulting
either to `0` would erase the difference between "reported at zero" and "not reported" —
and a gift or a grant really is reported at zero.

`is_derivative` is redundant with `source_table` on purpose: it lets a query filter
derivatives without knowing EDGAR's XML element names. A check constraint makes the two
impossible to contradict.

Indexed on `(transaction_date)`.

**`insider_reporting_owners`** — who signed a filing, and how they relate to the issuer.

| Column | Type | Notes |
|---|---|---|
| `id` | `integer` | primary key |
| `filing_id` | `integer` | required, FK to `sec_filings`, `ON DELETE CASCADE` |
| `reporting_owner_cik` | `text` | required, digits only |
| `owner_name` | `varchar(255)` | required |
| `is_director` | `boolean` | required, defaults to `false` |
| `is_officer` | `boolean` | required, defaults to `false` |
| `officer_title` | `varchar(255)` | nullable |
| `is_ten_percent_owner` | `boolean` | required, defaults to `false` |
| `is_other` | `boolean` | required, defaults to `false` |
| `other_text` | `varchar(255)` | nullable |

Unique on `(filing_id, reporting_owner_cik)`. The relationship columns mirror Form 4's
own `reportingOwnerRelationship` element — they are EDGAR's fields, not classifications
invented here.

Owners attach to the **filing**, not to a transaction. In Form 4 XML `reportingOwner` is
a sibling of the transaction tables rather than a child of any transaction, so a filing
with two owners and five transactions is *two owners and five transactions*: they are
joint filers, and EDGAR never says which of them a given row belongs to.

> **The trap this creates.** Joining transactions to owners through the filing **repeats
> each transaction once per owner** — two owners on a filing with five transactions
> yields ten rows from that join. Any total (shares, transaction counts, value) must come
> from the transactions side, or be taken over `DISTINCT insider_transactions.id`, and
> never from the fanned-out join. Nothing about a doubled total looks wrong once it is on
> screen, which is what makes it worth stating twice. `JointFilingTest` in
> `backend/tests/test_market_schema.py` demonstrates it against the real database.

### Added by the `0003` migration

The ingestion step gave the parser's output somewhere to live.

**`daily_prices`** — `provider_adjust_mode` (required) and `volume_adjustment` (nullable,
always NULL). Both described under "Adjustment metadata" below.

**`sec_filings`** — `document_type`, `schema_version`, `date_of_original_submission`,
`rule_10b5_1`, `holding_rows_skipped`, `remarks`, `footnotes` (JSONB), `source_xml`, and
`source_xml_sha256`. The document's own `document_type` sits beside the feed's `form_type`:
one is what discovery selected on, the other is what the XML says it is.

**`insider_transactions`** — `shares_owned_following`, `nature_of_ownership`,
`underlying_security_title`, `underlying_shares`, `exercise_price`, `expiration_date`, and
`footnote_refs` (JSONB). The existing `footnotes` TEXT column now holds the resolved text of
the footnotes that row references.

Typed columns wherever a value will be filtered or aggregated; JSONB only for the two
genuinely supplementary structures — the filing's footnote map and a row's field-level
references. Neither holds a Decimal, so nothing needs the string treatment prices get.

**Two column contracts were corrected to match the parser.** `insider_transactions.
ownership_direct_indirect` and the four relationship flags on `insider_reporting_owners`
became **nullable**, and the flags lost their `DEFAULT false`. The parser returns `None` when
a document omits one, and `NULL` says "the document did not say" where `false` would assert a
fact nobody stated. `rule_10b5_1` already worked that way.

**`ingestion_runs`** — `company_id`, `started_at`, `completed_at`, `parameters` (JSONB), and
`summary` (JSONB). One row per successful run, written inside the run's own transaction.

Every table `0003` touched was empty when it ran, which is why the added `NOT NULL` columns
needed no defaults and there were **no backfills**. That was checked before the migration was
written rather than assumed.

### Company disclosures

Three tables from `0004`, plus two small changes after it.

**`filing_documents`** — one row per document, because a filing has a primary document and
may have exhibits. `filing_id`, `document_name`, `document_type`, `role` (`primary`/`exhibit`),
`sequence`, `source_url`, `content_type`, `content`, `content_sha256`, `retrieved_at`,
`extracted_text`, `extraction_version`, `extraction_status`, `extraction_limitations`, and
`sections` (JSONB). Unique on `(filing_id, document_name)`.

`content` is the document as served; `extracted_text` is derived from it. They are stored
separately so the extraction can be improved and re-run without going back to the SEC — which
is the reason the original is kept at all. **No HTML goes in `sec_filings.source_xml`**; that
column keeps its Form 4 meaning.

**`company_fact_snapshots`** — `company_id`, `source_url`, `retrieved_at`, `content_sha256`
(unique), `byte_size`. One row per distinct fetch, deduplicated by hash.

**`financial_facts`** — `company_id`, `snapshot_id`, `taxonomy`, `concept`, `unit`, `value`
(`numeric`, unconstrained), `period_start` (nullable), `period_end`, `accession_number`,
`form`, `filed_date`, `fiscal_year`, `fiscal_period`, `frame`. Its unique key is declared
`NULLS NOT DISTINCT` for the reason given under "Observations are kept as observations".

**`0005` relaxed three Form 4-only columns on `sec_filings` to nullable.** A 10-K has no
ownership XML and no holding rows a parser could have skipped, so `source_xml`,
`source_xml_sha256` and `holding_rows_skipped` are NULL for it. An empty string would have
claimed there is XML and that it is empty; a zero would have implied a parser ran.

**`0006` added `sec_filings.report_date`.** Step 4 left it out because a Form 4's own XML
carries the authoritative period, but a 10-K has no XML and the feed's `reportDate` is the
only such date there is. Deliberately not backfilled: fetching it again to fill in the
existing Form 4 rows would be inventing metadata rather than recording something held.

`0005` and `0006` are separate migrations rather than edits to `0004`, which had already been
applied when those needs became visible. An applied migration is not edited.

## Prerequisites

Docker Desktop, with Compose v2 or later. Nothing needs to be installed locally — Python
and Node both run inside their containers.

## Setup

```bash
cp .env.example .env
```

The values in `.env.example` are **for local development only**. They are not secrets and
must not be reused anywhere reachable from an untrusted network. `.env` is gitignored.

## Configuration

Everything is read from `.env`.

| Variable | Required? | What it is |
|---|---|---|
| `POSTGRES_USER` `POSTGRES_PASSWORD` `POSTGRES_DB` | yes | the local database — see the notes at the top of `.env.example` |
| `TWELVE_DATA_API_KEY` | **no** | a Twelve Data key, read by the price client — see "Fetching daily prices" |
| `SEC_USER_AGENT` | **no** | the User-Agent EDGAR requires: application name plus a contact address — see "Fetching insider filings" |

Both are optional at startup, and that is deliberate: a missing provider setting must never
stop `/health`, `/portfolio`, or `/portfolio/valuation` from starting. The components that
do call a provider report their own missing configuration, in their own words, and only
when they are actually run:

```
error: No Twelve Data API key is configured. Set TWELVE_DATA_API_KEY in .env, then run: docker compose up -d backend
```

They reach the **backend container only**, and they are named explicitly in the `backend`
service's `environment:` block because Compose does *not* inject `.env` into containers by
itself — it uses that file for `${VAR}` substitution within `docker-compose.yml` and stops
there. Without those two lines the container would never see the values, and
`docker compose exec backend ...` could not reach them either.

The frontend service has no `environment:` block at all, so a key can never get to the
browser — a key shipped to the client is a key given away. The key is never logged either.

`SEC_USER_AGENT` in `.env.example` is an illustrative value and safe to commit, because a
User-Agent is not a secret. **Replace the address with your own before the first SEC
request.** EDGAR expects a contact it could actually reach, and the client refuses the
example value outright rather than sending it:

```
error: SEC_USER_AGENT is still the example value from .env.example. SEC's fair-access
policy expects a contact address that reaches a person, and may block requests without
one. Put a real address in .env, then run: docker compose up -d backend
```

That refusal is deliberate. SEC's fair-access policy treats a working contact as the thing
that keeps automated access tolerable; sending a placeholder is how a project gets blocked.

Your Twelve Data key belongs in the gitignored `.env`, never in `.env.example`. After
adding it, `docker compose up -d backend` so the container picks it up.

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

That runs every migration not yet applied. Today there are two — `0001`
(`portfolios`, `holdings`) and `0002` (`companies`, `daily_prices`, `sec_filings`,
`insider_transactions`, `insider_reporting_owners`) — for seven tables in all.
`alembic current` prints the revision the database is on:

```bash
docker compose exec backend alembic current
# 0002_market_data_and_filings (head)
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

`backend/tests/` covers the valuation calculation, the endpoint's error paths, the database
schema, the Twelve Data client, and the SEC EDGAR client and Form 4 parser, using the
standard library's `unittest` — no extra dependency to install:

```bash
docker compose exec backend python -m unittest discover -s tests -t .
# Ran 612 tests in 11.619s
# OK
```

`test_twelvedata.py`, `test_twelvedata_command.py`, `test_sec_edgar.py`, and
`test_fetch_sec_command.py` make **no network calls at all**. They hand the clients canned
HTTP responses through `httpx.MockTransport`, which is why those modules can test a read
timeout, a redirect off-host, or a body that is not JSON as easily as a 200.
`test_form4_parser.py` needs no transport at all — it reads fixture bytes.

`test_ingestion.py` is different again: it needs a real PostgreSQL, because what it checks is
what the database actually stores — that a seven-decimal price survives a round trip, that
`NULL` and `0` stay distinguishable, and that a re-run leaves row ids alone. It uses the same
isolated `alphadesk_test` database, and every test runs inside a transaction that is rolled
back, so nothing is ever committed there.

`test_chunking.py` is pure as well. `test_filing_index.py` and `test_filing_retrieval.py` use
a **real Qdrant running in-process** (`QdrantClient(location=":memory:")`) rather than a fake,
so the collection validation, the filters and the upsert semantics are genuinely exercised,
with only the embedding model replaced by a deterministic double. `test_htmltext.py` and
`test_company_facts.py` are pure too — HTML strings and JSON
payloads in, typed values out. `test_analysis.py` is pure — it takes plain values and needs
neither a database nor a transport. It covers both divergence directions, including `price_down_net_buying`, which the
stored NVDA sample cannot demonstrate on its own because it has sales and no purchases. That
branch is covered by a labelled fixture, never by manufacturing data.

The suite was run with **every non-Postgres connection blocked**, to confirm that: 594
passed. If a test ever reached the SEC or Twelve Data, it would fail there rather than
quietly depending on a live service.

`tests/fixtures/sec/` holds one **real** SEC filing and several **synthetic** ones, with a
README recording where each came from and which is which. The real one is stored verbatim
with its accession number and source URL.

`test_valuation.py` and `test_scenario.py` are pure arithmetic and need no database or
running service at all. The two endpoint files import `app.main` (so `DATABASE_URL` must
be set, which is why they run in the container) but use a stub session, so they open no
connection and cannot alter the demo portfolio. The `-100..+100` bound is validated by
Pydantic before the handler runs, so it is checked against the live server with the `curl`
commands above rather than from a handler test. The endpoint is also worth checking by hand with the
`curl` commands above, since those read the real seeded rows.

#### The schema tests, and their own database

`test_market_schema.py` and `test_migration_roundtrip.py` need a real PostgreSQL, because
what they check is that the *database* refuses bad rows — a duplicated daily bar, a bar
whose high is below its low, a negative volume. A mocked session cannot test a constraint.

They never touch the database holding your portfolio. `tests/testdb.py` derives a separate
one by appending `_test` to the name in `DATABASE_URL` (`alphadesk` → `alphadesk_test`),
refuses to continue unless that name matches `^[A-Za-z0-9_]+_test$`, and creates it on
first use. Every insert runs inside a transaction that is rolled back, so no test leaves a
row behind.

Two consequences worth knowing:

- **Running the suite creates `alphadesk_test` the first time.** It is a normal database
  in the same container, separate from your data. `docker compose exec postgres dropdb -U
  alphadesk alphadesk_test` removes it; the next run recreates it.
- **The schema there is built by running the real Alembic migration**, not by
  `Base.metadata.create_all`. That means the tests cannot pass against a schema the
  migration would not actually produce, and it is what makes the downgrade round trip
  meaningful. `test_migration_roundtrip.py` downgrades to `0001` and back — and does it
  **only** against `alphadesk_test`. A downgrade is the one migration operation that can
  destroy data, so it is not run against the development database at any point.

`frontend/src/**/*.test.ts(x)` covers the display formatting and the page's states, with
`fetch` stubbed. Every failure, empty, and zero-value case is a fixture — none of them
edits the seeded portfolio to provoke a state:

```bash
docker compose exec frontend npm test
# Test Files  5 passed (5)
#      Tests  61 passed (61)
```

## Fetching daily prices (Twelve Data)

A read-only command that pulls recent daily bars for one US stock and prints them.

```bash
docker compose exec backend python -m app.fetch_prices
docker compose exec backend python -m app.fetch_prices --symbol NVDA --exchange NASDAQ --bars 30
```

`NVDA` on `NASDAQ`, 30 bars, is the default, so the bare command is a working example.
Real output, trimmed to the last entries:

```json
{
  "symbol": "NVDA", "exchange": "NASDAQ", "currency": "USD",
  "provider": "twelve_data", "interval": "1day",
  "adjustment_basis": "adjusted", "provider_adjust_mode": "splits",
  "exchange_timezone": "America/New_York",
  "retrieved_at": "2026-09-18T15:26:28.419572+00:00",
  "requested_bars": 30, "returned_bars": 30,
  "first_date": "2026-08-06", "last_date": "2026-09-17",
  "sample_bars": [
    {"date": "2026-09-16", "open": "214.14000", "high": "216.75999",
     "low": "212.5", "close": "213.89999", "volume": 96563600},
    {"date": "2026-09-17", "open": "218.38000", "high": "219.91000",
     "low": "217.14999", "close": "219.34000", "volume": 93960500}
  ]
}
```

**This fetches. It does not store.** Nothing is written to `daily_prices`, no database
session is opened, no portfolio is touched, and the command is not wired into startup.
Persisting bars is Step 4, after the EDGAR client in Step 3. The gap is deliberate: a
client that has never been run by hand is a poor thing to debug through an ingestion
pipeline.

### Decimal values are strings

Every price is a JSON **string**, for the reason in "Why decimals are strings" below — a
JSON number is a double, and parsing one back would lose the precision the
`Decimal`-and-`NUMERIC` arrangement exists to protect. `volume` stays a JSON number,
because a share count is a count.

### Dates are exchange session dates

`datetime` from the provider is read as a **calendar date in the exchange's timezone**
(`America/New_York` here), never as a UTC-midnight instant. Converting it to a timestamp
would invent an hour the provider never stated.

**The current session is excluded, and any later date with it.** A bar dated today is a
session still in progress — on the live run above, the current day carried roughly 1% of
the previous day's volume — so letting it through would feed a partial session into later
calculations as though it were complete.

The cost is real and deliberate: **a session that has genuinely closed is also excluded
until the exchange's calendar day rolls over.** Ask for 30 bars on a Tuesday afternoon and
the most recent one you get is Monday. That is the conservative direction to be wrong in.

The client asks for two spare bars to cover this, which is why 32 are requested when 30
are wanted. Those spares are trimmed back off afterwards, so `returned_bars` never exceeds
`requested_bars` — asking for thirty and being handed thirty-one is a surprise however it
arose. If fewer completed sessions exist than were requested, the real count is reported
and nothing is invented or forward-filled.

### Adjustment basis

Prices are requested with `adjust=splits` — split-adjusted, an explicitly documented mode,
recorded as `provider_adjust_mode`. The result's `adjustment_basis` is `"adjusted"`, which
is the vocabulary `daily_prices.adjustment_basis` is constrained to.

**Volume is not assumed to be adjusted just because prices are.** The provider's treatment
of volume under `adjust=splits` has not been verified, so `volume` should be treated as
reported until Step 4 establishes otherwise.

Passing `dp` to ask the provider to round is not done, and should not be: it makes things
worse. A live comparison showed `dp=6` re-deriving values that were clean, turning an open
of `213.38000` into `213.380005`.

### Prices keep every digit the provider sent

Twelve Data occasionally returns a price with more decimal places than expected. In a
120-session NVDA window, **6 of 480 prices** carried a seventh decimal place, several with
a repeated trailing pattern — `225.0099945`, `190.0099945`, `197.0099945`. These are
provider-returned precision artifacts; nothing here speculates about what produces them.

They are passed through **unchanged**: not rounded, and not grounds for refusing a bar.
`daily_prices.open/high/low/close` are `numeric(18,6)`, so a value needing a seventh
decimal place **cannot be inserted as the schema stands.** That mismatch is real,
documented here, and deliberately unresolved — Step 4 will either round on ingest under a
stated policy or widen the column in a migration. Rejecting such a bar instead would have
looked tidier, but it makes the client fail on roughly one NVDA session in twenty, which is
not a client anyone can use.

### When it fails

Exit status is `0` on success and `1` on a failure it understands, with a sanitized
explanation on stderr and **no traceback** — a traceback is exactly where a leaked request
URL would surface. A genuine bug is left to raise, because a crash that hides behind
"something went wrong" is harder to fix than one that shows its trace.

| Situation | Reported as |
|---|---|
| No key configured | `MissingApiKeyError`, raised before any connection is opened |
| Key rejected, or plan does not cover it | `ProviderAuthError` |
| Unknown ticker, or not listed on that exchange | `SymbolNotFoundError` |
| Rate limit or exhausted credits | `RateLimitError` |
| Timeout, refused connection, provider 5xx | `ProviderUnavailableError` |
| Unreadable body, or a response about another instrument | `MalformedResponseError` |
| Finite/positive/OHLC or volume rules broken | `DataValidationError` |
| Bad arguments | `InvalidRequestError` |

**There are no retries.** A rate limit stops the command rather than quietly spending more
credits, and the message says so. Failures are checked on both the HTTP status and the
payload, because Twelve Data can return an error inside an HTTP 200. The mappings above
were confirmed against the live API — an unknown ticker, a symbol asked for on the wrong
exchange, and a rejected key — not only against fixtures.

### The key never reaches the URL

The key travels in the `Authorization: apikey …` header, never as the `apikey` query
parameter, so it cannot appear in a request line, a log, or an exception message. httpx
puts the request URL in its own exception text, so those exceptions are caught and
re-raised with `from None`; the original is never chained, which is what keeps it out of a
traceback. Provider messages go through a redaction pass, and the command redacts once
more before printing, because it is the last point before a person reads the text.

## Fetching insider filings (SEC EDGAR)

A read-only command that finds a company's recent Form 4 filings, fetches each one's
ownership XML, and prints the owners and transactions inside it.

```bash
docker compose exec backend python -m app.fetch_sec
docker compose exec backend python -m app.fetch_sec --cik 0001045810 --limit 3
```

`--cik` defaults to `0001045810` (NVIDIA) and `--limit` to 3, capped at 10. The default
CIK is **checked against SEC metadata** rather than trusted: the feed's ticker list must
still contain `NVDA`, or the command stops and says so. A CIK that meant NVDA when the
constant was written is not evidence that it means NVDA now.

**This fetches. It does not store.** Nothing is written to `sec_filings`,
`insider_transactions`, or `insider_reporting_owners`, no database session is opened, and
the command is not wired into startup. Persisting is Step 4.

### What discovery actually covers

Only the **`filings.recent`** list from
`https://data.sec.gov/submissions/CIK##########.json` is searched. For NVIDIA that is
**1001 filings**. Older filings live in separate submission files — **1478 more, from
1998-03-06 to 2020-07-11** — and this milestone does not page through them.

The command says so in its own output rather than leaving the limit implied:

```json
"scope": "SEC submissions 'recent' list only: 1001 filings scanned; 1478 older filings
          held in separate submission files were NOT searched"
```

Form `4` and `4/A` are selected, deduplicated by accession number, and returned newest
first with the accession number as a tiebreak so the order is the same on every run. No
match is a valid empty result, not an error.

### Finding the document is the hard part

Form 4's `primaryDocument` in the submissions feed is an **XSL-rendered** path:
`xslF345X06/wk-form4_1789160684.xml`. Fetching that returns a rendered page, not ownership
XML. The accession's directory index has the same problem in reverse — it serves HTML. And
**both answer HTTP 200**.

So the client trusts none of it. It takes the last path segment of `primaryDocument` (which
is what strips the render prefix) and accepts the result only if the body actually parses
as XML with an `ownershipDocument` root. Content type is not consulted. If that fails, it
falls back to the accession's `index.json`, considers only `.xml` entries, and tries **at
most three** of them — a directory listing this client does not control is not something to
walk blindly.

```bash
docker compose exec backend python -m app.fetch_sec --limit 1
```

### Returned data

Per filing: accession number, form type, amendment flag, filing date, acceptance
timestamp, retrieval timestamp, source XML URL, issuer CIK/name/trading symbol, schema
version, period of report, `dateOfOriginalSubmission` when the document states one,
the Rule 10b5-1 indicator, `remarks`, the resolved footnotes, every reporting owner, and
every transaction.

Owners are a **list beside** the transactions, never spread across them. In Form 4 XML
`reportingOwner` is a sibling of the transaction tables, so a filing with two owners and
five transactions is two owners and five transactions. Nothing assigns an owner to a row,
because the document does not.

Transactions come from both tables, each with its `source_table` and a 0-based
`row_position` **within its own table** — the identity Step 1's unique key expects.
`<nonDerivativeHolding>` and `<derivativeHolding>` rows are **not** transactions: they
report positions that already existed. They are excluded by name and counted in
`holding_rows_skipped`, so the exclusion is visible. Real NVDA filings showed two such
rows, which is why the count exists.

Three properties are preserved deliberately, because they are the ones a careless parser
loses:

- **Absent is not zero and not false.** A missing price is `null`; a price reported as `0`
  is `"0"`. `rule_10b5_1` is `null` when the document does not say, and `false` when it
  says no. Real filings contained both a `0` price (an RSU award) and a `true` indicator.
- **Fields are not classified.** Transaction codes are kept as EDGAR wrote them — `A`, `S`,
  `G`, `M`, `F` — never reduced to "buy" or "sell". The parser does not compute divergence,
  and does not decide what a code means.
- **Nothing is inferred.** An amendment records `dateOfOriginalSubmission` only when the
  document states one. Nothing guesses which accession it amends, so a Form 4 and its
  `4/A` stay two unrelated filings until something deliberately links them.

Decimal values are printed as JSON **strings**; a JSON number is a double. **No precision
limit is applied anywhere** — see the Step 2 discussion of `numeric(18,6)`, which applies
here too and is Step 4's problem to settle.

### When it fails

Exit `0` on success, `1` on an understood failure, with a concise message on stderr and no
traceback.

| Situation | Reported as |
|---|---|
| `SEC_USER_AGENT` missing, blank, or still `example.com` | `MissingUserAgentError`, before any request |
| Bad CIK or out-of-range limit | `InvalidRequestError`, before any request |
| SEC refuses the request (401/403) | `AccessDeniedError` |
| Rate limited (429) | `RateLimitError` |
| No company under that CIK | `CompanyNotFoundError` |
| No readable ownership XML in the archive | `DocumentNotFoundError` |
| Timeout, refused connection, SEC 5xx | `ProviderUnavailableError` |
| Body over 8 MiB | `ResponseTooLargeError` |
| Unreadable JSON, HTML where XML was required | `MalformedResponseError` |
| Document is about a different company | `IssuerMismatchError` |
| A transaction row cannot be read | `NumberParseError` / `MalformedDocumentError` |

**There are no retries.** A 429 stops the run. Nothing here rotates proxies or impersonates
a browser to get around a block — if SEC says no, the answer is no.

### Respecting SEC

One `RateLimiter` is shared by discovery **and** document fetching, so the whole command
stays under **two requests per second** — well inside SEC's published limit. Requests are
sequential, connections have a 10s connect / 30s read timeout, bodies are streamed with a
cap so the limit bounds the download rather than rejecting it afterwards, and the
connection pool is always closed.

The key never appears in a URL because **the SEC needs no key**. It requires a User-Agent
naming your application and a contact address, which is why `SEC_USER_AGENT` must be a real
value: it is sent on every request so SEC can reach you if your requests cause a problem.
That is also why it is not treated as a secret.

Redirects are followed **one hop at a time, validated before each is requested**, rather
than handed to httpx — following first and checking afterwards would mean the request had
already gone out. Only HTTPS, only `www.sec.gov` and `data.sec.gov`.

### The XML parser is deliberately plain

Python's stdlib `ElementTree`, no extra dependency. It refuses external entities outright —
an external `SYSTEM` entity raises `ParseError: undefined entity` and reads nothing — and a
fixture and a test pin that property so a future change cannot quietly lose it. Namespaces
are matched on local element names, so a namespaced document parses exactly like the
un-namespaced ones EDGAR actually serves.

A transaction row that cannot be read **fails the whole document**. Skipping it would
return a filing that looks complete and is quietly missing a trade.

### What the parser returns that the tables could not hold

This section used to list fields with nowhere to go — `date_of_original_submission`, the
derivative details, `shares_owned_following`, `nature_of_ownership`, and the field-level
footnote attribution. **The `0003` migration gave all of them columns.** See "Storing what
was fetched" below.

## Storing what was fetched (ingestion)

The first command that writes. It fetches a bounded NVDA dataset and saves it.

```bash
docker compose exec backend alembic upgrade head
docker compose exec backend python -m app.ingest_nvda --bars 30 --filings 3
docker compose exec backend python -m app.ingest_nvda --dry-run
```

`--bars` defaults to 30 (max 500) and `--filings` to 3 (max 10), both from the clients' own
bounds. **Running it again with identical source data changes nothing and inserts nothing.**

### Adding a company other than NVIDIA

`app.ingest_nvda` is a fixed-identity wrapper around `app.company_ingestion.run`;
`python -m app.ingest_company` is the general command, and both go through the same module.
Nothing in that module is specific to an issuer. The company is **named, never guessed** — all
three of `--symbol`, `--exchange` and `--cik` are required, because identity is the thing the
command establishes and a default for any of them would be an assumption about which company
was meant.

```bash
docker compose exec backend python -m app.ingest_company \
  --symbol AAPL --exchange NASDAQ --cik 0000320193 --bars 30 --filings 3
docker compose exec backend python -m app.ingest_company \
  --symbol AAPL --exchange NASDAQ --cik 0000320193 --dry-run
```

The CIK is normalized to ten digits, so `320193` and `0000320193` are the same issuer and the
leading zeros EDGAR writes are preserved rather than lost.

**Identity is checked against three things, and all three have to line up:** what the command
asked for, what SEC metadata says about that CIK, and what the price provider answered about.
A disagreement aborts the run before the write transaction opens, so a refusal leaves the
database exactly as it was.

**The schema holds one listing per issuer** — `companies` is unique on `sec_issuer_cik` — so
an issuer with several listed share classes is refused rather than stored with one of them
silently lost:

```bash
docker compose exec backend python -m app.ingest_company \
  --symbol GOOGL --exchange NASDAQ --cik 0001652044
# error: CIK 0001652044 reports more than one listed ticker (GOOG, GOOGL, GOOGM, GOOGN).
# This schema holds one listing per issuer, so these share classes cannot be stored without
# losing one of them. Nothing was written.
```

That refusal is decided from SEC metadata **before the price provider is asked anything**, so
an unsupported company costs no request from the price quota. `test_company_ingestion.py`
pins the order, and it was checked live: with the price API key deliberately set invalid, the
GOOGL command above still failed on the CIK's ticker list — never reaching the price
provider — while the same command for AAPL, a supported company, failed on the price
provider's 401. The refused company spent nothing.

Adding a company is three commands, run by hand, in this order:

```bash
docker compose exec backend python -m app.ingest_company \
  --symbol AAPL --exchange NASDAQ --cik 0000320193      # prices and Form 4 filings
docker compose exec backend python -m app.ingest_company_context \
  --symbol AAPL --as-of 2026-09-17                      # 10-K, 10-Q, 8-Ks, financial facts
docker compose exec backend python -m app.index_filings --symbol AAPL    # chunk and embed
```

and then `search_filings` and `analyze_insiders` work for it, each filtered to that company.
**Measured, adding AAPL to a database that already held NVDA:**

| | |
|---|---|
| Prices and filings | 1 company, **30** daily prices, **3** Form 4 filings, 3 owners, 6 transactions |
| Discovery scope | 1001 filings scanned; 1247 older ones not searched |
| Company context | **3** filings, **4** documents (3 primary, 1 exhibit), **26** facts, 5 concepts matched, `revenue` unmatched |
| Context requests | **6 of 24**; coverage complete, 2 amendments reported and not applied |
| Index | **4** documents, **193** chunks |
| Retrieval | `--query "What does the company disclose about supply chain and manufacturing concentration?"` → 10-K `0000320193-25-000079`, `financial_statements`, similarity 0.677 |
| Insider analysis | `insufficient_coverage`, 1 run of 3 filings, price **+7.87%** against net reported selling |
| NVDA afterwards | **every row byte-identical** — all nine tables, same row counts and SHA-256s |
| Qdrant afterwards | **350** NVDA points still present; 543 total = 350 + 193 |

Every default symbol on `index_filings`, `search_filings` and `analyze_insiders` is still
`NVDA`, so a command run without `--symbol` still means the company the project was developed
against. Adding a second company changed no default.

### Order of operations, which is the point

Everything is fetched over HTTP *first*, then validated, and only then is a database
transaction opened. A network failure therefore cannot leave half a filing behind, and the
transaction stays short enough to hold a lock without anyone noticing.

Both sources must agree about which company this is before a single row is written: the
price series must be `NVDA` on `NASDAQ`, and SEC metadata must still list `NVDA` for CIK
`0001045810`. A disagreement aborts the run.

The two checks are interleaved with the two fetches rather than run together at the end —
**SEC metadata first, its answer checked, and only then the price provider**. An issuer the
schema cannot hold is refused without spending a request from the price quota.

One distinction worth stating: a SEC request that **succeeds and returns no filings is not a
failure**. Prices are still valid and still stored; the summary reports zero filings and
warns that an absence of filings is not evidence of no insider activity. Only a failed
*request* aborts.

`--dry-run` fetches, validates, classifies every row, and reports what *would* change —
including any conflict — without writing anything or opening a transaction.

### Precision: nothing is rounded, anywhere

`daily_prices.open/high/low/close` and `insider_transactions.shares/price_per_share` are
plain `NUMERIC`, not `numeric(18,6)`. Step 2 found Twelve Data returning seven-decimal
prices; rounding them on the way in would discard real data while leaving everything
downstream looking perfectly normal.

Every existing CHECK survives — `low > 0`, `high >= low`, the open/close range checks,
`volume >= 0`, `shares > 0`, `price_per_share >= 0`. Only the column *type* was loosened.

The live ingestion stored this, as-is:

```
 trading_date |    close    | scale
--------------+-------------+-------
 2026-08-17   | 225.0099945 |     7
```

**`portfolios.cash_balance`, `holdings.quantity`, and `holdings.average_buy_price` were not
touched.** They hold the demo portfolio's money and positions, and the demo valuation depends
on them behaving exactly as before.

### Adjustment metadata

`provider_adjust_mode` records the provider's exact mode (`splits`) beside
`adjustment_basis`, our own two-word vocabulary (`adjusted`). **Both are in the unique key**
alongside company, date, and provider, so two adjustment modes for one day cannot collide or
overwrite each other.

`volume_adjustment` exists and is **NULL on every row**, because Twelve Data states nothing
about volume adjustment. NULL means *not stated*, which is a different fact from *not
adjusted*, and the column exists so the difference can be recorded rather than collapsed.

### Repeat runs, and what a conflict means

Every write is `INSERT ... ON CONFLICT (identity) DO NOTHING`, so the **unique constraint**,
not application logic, is what makes duplicates impossible. When no row is inserted, the
stored row is read back and its business columns compared:

| Incoming vs stored | What happens |
|---|---|
| identical | counted **unchanged**; nothing is written, so the row id and its original `retrieved_at` survive |
| different | **`IngestionConflictError`**, naming the record and the columns that differ |

A conflict is **not** overwritten and **not** ignored. Overwriting would destroy the record
of what was stored first; ignoring would leave the database quietly disagreeing with the
provider. Both are worse than stopping.

`retrieved_at` is provenance, not a business value — it differs on every run by definition,
so it is excluded from the comparison and never updated.

A live run followed immediately by a second produced exactly this:

```
run 1:  companies 1 inserted   daily_prices 30 inserted   sec_filings 3 inserted
        insider_transactions 9 inserted   insider_reporting_owners 3 inserted
run 2:  companies 0 inserted, 1 unchanged   daily_prices 0 inserted, 30 unchanged
        sec_filings 0 inserted, 3 unchanged   ... ids 1..30 and retrieved_at unchanged
```

> **Provider corrections, revised split history, and parser reprocessing all land in the
> conflict case, and all need a refresh policy this milestone does not have.** Failing loudly
> is the honest placeholder for it. A conflict means "look at this deliberately", not "run
> it again".

### Concurrent runs

The first statement in the transaction is
`SELECT pg_advisory_xact_lock(hashtext('alphadesk:ingest:<cik>'))`. Two runs for the same
company serialise: the second waits, then finds the first's rows already committed and
reports them unchanged. Runs for **different** companies do not block each other. A run that
dies releases the lock with its transaction.

### Amendments are stored, not linked

A Form 4 and its `4/A` are **two separate filings**. `amends_filing_id` is left NULL always,
because the document never says which accession an amendment replaces and inferring it from
owner, dates, or similar amounts would be a guess.

The summary warns whenever amendments are present, because **raw stored transactions are not
safe to sum**: adding a Form 4 and its `4/A` together double-counts what they share. Step 5
must resolve that before computing any insider total.

### Coverage, stated in the data

Each run writes one row to `ingestion_runs` recording when it ran, what it asked for, and
what it found — including the discovery scope. It is written inside the same transaction, so
a failed run leaves no receipt, because nothing it wrote survived either. It is a receipt,
not a job queue: no states, no retries, no scheduler.

**Three filings are a bounded sample, not a month of insider activity.** The summary says so
in `warnings`, and so does the stored run record.

### Inspecting what was saved

No credential is needed for any of this — the data is in the database, and the commands read
it with `psql` inside the container.

```bash
docker compose exec postgres psql -U alphadesk -d alphadesk -c \
  "SELECT trading_date, open, high, low, close, volume, provider_adjust_mode
     FROM daily_prices ORDER BY trading_date DESC LIMIT 5;"

docker compose exec postgres psql -U alphadesk -d alphadesk -c \
  "SELECT accession_number, form_type, filing_date, rule_10b5_1, holding_rows_skipped
     FROM sec_filings ORDER BY filing_date DESC;"

docker compose exec postgres psql -U alphadesk -d alphadesk -c \
  "SELECT f.accession_number, t.transaction_code, t.acquired_disposed,
          t.shares, t.price_per_share, t.footnotes
     FROM insider_transactions t JOIN sec_filings f ON f.id = t.filing_id
    ORDER BY f.filing_date DESC, t.row_position;"

docker compose exec postgres psql -U alphadesk -d alphadesk -c \
  "SELECT started_at, summary->'counts' FROM ingestion_runs ORDER BY started_at;"
```

The source XML is kept per filing, with its SHA-256, so a parse can be re-checked later
without going back to the SEC:

```bash
docker compose exec postgres psql -U alphadesk -d alphadesk -tAc \
  "SELECT source_xml FROM sec_filings WHERE accession_number = '0002152188-26-000005';"
```

## Analysing insider activity against price

Read-only. Nothing is written and nothing is fetched — every statement is a `SELECT`, and an
analysis that could fetch its own missing data would be able to manufacture the answer it was
looking for.

```bash
docker compose exec backend python -m app.analyze_insiders --symbol NVDA
docker compose exec backend python -m app.analyze_insiders --symbol NVDA \
  --start 2026-08-06 --end 2026-09-17
```

Both dates default to the stored price range, and **the result reports the dates it actually
used** alongside the ones requested, so the two cannot be confused.

### What it computes

```
price_change_percent = (last_close / first_close - 1) × 100

reported_value       = reported_shares × reported_price_per_share
net_reported_value   = purchase_value - sale_value
```

`first_close` and `last_close` are the first and last closes **inside the requested range**,
from **one** price series. Series are never combined: raw and adjusted prices are different
numbers for the same day, and a change computed across them would be an artefact of mixing
them. A second series makes the command refuse and say so rather than pick one.

Fewer than two price dates gives an unavailable metric **with a reason**, never a zero. No
session is forward-filled, and the presence of some bars is not treated as a complete trading
calendar.

The reported values are **estimates from reported prices**, which may themselves be weighted
averages or rounded. They are not exact cash flows. Transaction shares are never multiplied by
a current or split-adjusted market price: the SEC's own share-and-price pair is used, because
the alternative invents a number the filing never contained.

### Which transactions count

Only **non-derivative transactions in a supported common-share class**, and only two shapes:

| Included | |
|---|---|
| `P` acquired | a purchase |
| `S` disposed | a sale |

**`P` and `S` are not synonyms for "open market".** They cover open-market *and* private
transactions, so nothing in the output is labelled open-market — Form 4 does not say that.

The security-title allowlist is explicit and normalised: `common stock`, `common`. It holds
both because NVDA's own stored filings use both for the same class, and a mapping that knew
only one would have dropped a real row without saying so.

Everything else is excluded **with a counted reason**:

| Reason | What it covers |
|---|---|
| `derivative_security` | the derivative table |
| `code_not_purchase_or_sale` | grants `A`, gifts `G`, exercises `M`, withholding `F`, and the rest |
| `security_class_not_supported` | a title outside the allowlist |
| `inconsistent_code_direction` | `P` disposed or `S` acquired — a data-quality signal, not something to reinterpret |
| `transaction_date_outside_price_range` | a trade outside the dates being compared |
| `filed_after_cutoff` | accepted at or after the information cutoff |
| `acceptance_time_unknown` | no acceptance timestamp, so the cutoff cannot be applied |

**Transactions are read without joining reporting owners.** Owners attach to the filing, so
joining them would return each transaction once per owner and multiply every count and total.
The accession number is carried instead, so an owner can be looked up deliberately.

### The information cutoff

The end date is an end-of-day cutoff in `America/New_York`, implemented as the **exclusive
start of the following local day, converted to UTC** — so `2026-09-17` becomes
`2026-09-18T04:00:00+00:00` in September and `…T05:00:00+00:00` in January, following daylight
saving.

A transaction counts only when its date falls inside the actual price comparison dates **and**
its filing was accepted before that instant. A trade submitted later is not used merely
because its transaction date is earlier.

This is **retrospective analysis over currently stored data, not a point-in-time backtest.**
Later provider revisions and adjusted price histories have not been reconstructed, so the
prices are as they stand today rather than as they stood then.

### Amendments

If **any** stored `4/A` for the company was accepted before the cutoff, the net insider
direction and the divergence comparison are **withheld**, and the accessions are listed.
Price metrics and clearly labelled raw transaction detail are still returned.

The policy is deliberately broad. Deciding which original transactions an amendment corrects
would be a guess, and guessing there would corrupt exactly the number the analysis is about.
An amendment accepted *after* the cutoff does not affect an earlier period.

### Coverage is not the conclusion

Two separate fields, and the separation is the point:

- **`sample_comparison`** — what these records show: `price_up_net_selling`,
  `price_down_net_buying`, `same_direction`, `no_directional_difference`,
  `no_eligible_transactions`, or `unavailable`.
- **`overall_conclusion`** — **`insufficient_coverage`** whenever coverage is partial or
  unknown, which today is always.

A comparison can be perfectly calculable and the overall conclusion still be that the evidence
does not support one. Coverage comes from the `ingestion_runs` receipts, never from the oldest
and newest filing dates — an absence of filings is not evidence of an absence of activity.

> **"No purchases in these filings" is never "insiders made no purchases during this
> period."** An unimported or unsampled filing could contain one.

The rule is descriptive and is described that way in the output. There is no confidence score,
no price target, no profitability claim, and no "strong bullish/bearish" label, because none
of those follow from what these records say.

### Decimal handling

Every monetary and percentage value is serialised **exactly**, as a string. A separate
`display` block carries the same values rounded to 2 decimal places with `ROUND_HALF_UP` —
**the only rounding anywhere**, and it is never used to decide a sign or a comparison. A price
change that rounds to `0.00` but is not zero is still reported as a direction.

### What the stored NVDA data actually shows

One real run over all 30 stored bars and 3 stored filings:

| | |
|---|---|
| Period | 2026-08-06 → 2026-09-17 (30 observations) |
| Series | `twelve_data` / `adjusted` / `splits` |
| Price | `218.99001` → `219.34000`, **+0.159820%** |
| Eligible | **0 purchases, 7 sales**, $235,636,867.41 sold |
| Net reported value | **−$235,636,867.41** |
| `sample_comparison` | **`price_up_net_selling`** |
| `overall_conclusion` | **`insufficient_coverage`** |
| Excluded | 2 rows, both `code_not_purchase_or_sale` (one gift, one grant) |

The price rose slightly while the insiders in these three filings were net sellers. That is a
description of seven rows, not a signal, and the overall conclusion says so in the same
result.

### Portfolio exposure is not calculated

NVDA and AAPL have been ingested; MSFT has not. Valuing the portfolio against market data
would need consistently dated prices for every holding, so it is not attempted, and the demo
valuation is untouched. Nothing here reads the stored bars.

## Company disclosures and selected financial facts

Steps up to here gave the analysis numbers. This gives it prose — what the company says about
itself — and a handful of headline figures, so a later step can put insider activity in
context.

```bash
docker compose exec backend alembic upgrade head
docker compose exec backend python -m app.ingest_company_context --symbol NVDA --as-of 2026-09-17
docker compose exec backend python -m app.ingest_company_context --symbol NVDA --dry-run
```

`--as-of` defaults to today and is always printed. It sets the same end-of-day
America/New_York cutoff the insider analysis uses, so both mean the same instant by "the end
of that day".

### What it selects, and what it does not claim

| Selected | Rule |
|---|---|
| 10-K | the latest **original** accepted before the cutoff |
| 10-Q | the latest **original** accepted before the cutoff |
| 8-K | up to **three** latest originals from the **90 days** before the cutoff |

Source is the recent submissions list only, and **a form type that is not in that window is
reported as a limit of the search, never as the company not having filed one.** Those are
different statements and the output keeps them apart.

`10-K/A`, `10-Q/A` and `8-K/A` entries accepted before the cutoff are listed as **amendment
warnings**. Nothing is replaced or merged; amendment resolution is not implemented.

The three 8-Ks are described as the most recent, not as the material ones. Nothing here knows
which events mattered.

### Documents and exhibits

Each selected filing's primary document is fetched. For an 8-K, the filing's own index page
is read for its exhibit table, and exhibits are chosen by a fixed rule: **type `EX-99`**, HTML
or text only, ordered by exhibit number, **at most two per filing**.

The primary 8-K is not assumed to be the earnings release. On the stored sample it is not: the
2026-08-26 8-K's press release is `q2fy27pr.htm` (EX-99.1) beside it, while the 2026-09-03 8-K
has no exhibit at all.

PDFs, images, XBRL taxonomy files and the `.txt` complete-submission file are **not fetched**,
and every omission is reported with its reason. No claim of complete exhibit coverage is made.

**Two explicit limits.** Each document must fit the client's existing 8 MiB cap, and the run
is bounded by a **request budget of 24**; the stored run used 12. Reaching either marks the
run's coverage incomplete and says so rather than quietly returning a smaller set.

> Note: `index.json` cannot be used to find exhibits. Its `type` field reports `text.gif` for
> HTML, images and XML alike. The real types are only in the index *page*.

### How the text is extracted

`backend/app/htmltext.py` — standard library `html.parser`, no new dependency. What is
dropped and what is kept is the whole design:

| Dropped | Kept |
|---|---|
| `<script>`, `<style>` | visible inline-XBRL text |
| anything with `display:none` (398 in the stored 10-K) | headings and paragraph breaks |
| `<ix:hidden>` and `<ix:header>` — the XBRL context blocks | table row and column boundaries |
| comments | units, negative signs, table headers |

The distinction that matters: inline XBRL tags a number **where it is already displayed**, so
its text is kept, while the hidden block that declares contexts is removed. The same figure
appears once, not twice.

Tables keep tabs between cells and newlines between rows. A financial table flattened into one
line of words is no longer a table, and a number in it can no longer be attributed to its
column.

### Sections, and the contents-page trap

Every large filing states its Item headings **twice**: once on the contents page and once
where the section begins. A first-match rule takes the contents page every time — measured on
the stored 10-K, `Item 1A. Risk Factors` matches **eight** times, four of them within 500
characters around offset 25,000.

So the contents page is treated as what it is: **the densest cluster of headings in the
document**, identified and removed as one block before any section is chosen. On the stored
10-K that gives `business` at 10,372 where a naive rule gives 4,998, plus `risk_factors`,
`management_discussion` and `financial_statements` — all four, correctly.

Positions are stored as offsets into the extracted text. **A section that cannot be found is
reported as not detected**, and the full text is kept regardless. Nothing is discarded for
want of a section, and no claim of perfect extraction is made.

### The financial concepts

Six concepts are kept, and no more:

| Meaning | Concept | Kind |
|---|---|---|
| Revenue | `Revenues` | duration |
| Revenue, earlier tag | `RevenueFromContractWithCustomerExcludingAssessedTax` | duration |
| Net income (loss) | `NetIncomeLoss` | duration |
| Total assets | `Assets` | instant |
| Total liabilities | `Liabilities` | instant |
| Cash and cash equivalents | `CashAndCashEquivalentsAtCarryingValue` | instant |

**Both revenue concepts are stored, as separate concepts.** They are different tags with
different definitions: NVDA used the second through fiscal 2022 and the first since. Adding
them, or treating one as a fallback for the other, would produce a revenue series that is
neither.

**There is no non-negative constraint on any value.** Net income is negative in a loss-making
period, and a constraint that assumed otherwise would reject a real filing.

JSON numbers are parsed with `parse_float=Decimal`, so a decimal value never passes through a
binary float on the way in.

### Observations are kept as observations

Nothing is collapsed into one value per concept and end date. The stored sample shows why:

```
 concept  |    value     | period_start | period_end | fiscal_period |  frame
----------+--------------+--------------+------------+---------------+----------
 Revenues |  96221000000 | 2026-04-27   | 2026-07-26 | Q2            | CY2026Q2
 Revenues | 177837000000 | 2026-01-26   | 2026-07-26 | Q2            |
```

The same end date. The same fiscal period. **A quarter and a year-to-date total.** Only
`period_start` tells them apart, which is why it is in the natural key and why `fp` is stored
as metadata rather than used as identity. Both appear in the filing's own income statement,
in the two columns headed "Three Months Ended" and "Six Months Ended".

**Natural key:** `(company_id, taxonomy, concept, unit, period_start, period_end,
accession_number)`, declared `UNIQUE NULLS NOT DISTINCT` — because an instant fact such as
`Assets` has no `period_start`, and PostgreSQL treats every NULL as distinct by default, which
would re-insert the whole balance sheet on every run.

The same period reported by two filings is two observations, both kept. Nothing derives a
fourth quarter, a growth rate, a ratio, or "the latest financials".

> **Company Facts is fetched now.** It is labelled in the output as a *current-source snapshot
> filtered to the selected filings* — not a record of what the endpoint returned on the
> analysis date.

### Repeat runs

The Step 4 conventions, reused: fetch and validate before the transaction opens, then

| | |
|---|---|
| Identical document or fact | no duplicate; row id and original `retrieved_at` preserved |
| Different **document content** under the same identity | explicit conflict |
| Re-extraction alone | not a conflict — the document is its content, the text is derived from it |
| Changed Company Facts response | a **new snapshot**, never a conflict |
| Identical snapshot content | deduplicated by `content_sha256` |

`--dry-run` fetches, validates, classifies and reports, including any conflict, and writes
nothing.

### Coverage stays separate from insider coverage

Disclosure runs are recorded in `ingestion_runs` under `scope = 'company_context'`, and the
insider analysis reads **only** `scope = 'nvda_form4'` runs. It also reads only
`form_type IN ('4','4/A')` filings.

Both filters exist because both failure modes are real. Without the first, a disclosure run
becomes "the latest run" and its three filings are reported as insider-history coverage;
without the second, a 10-K row sits one join away from being counted as an insider trade.
`test_disclosure_does_not_affect_insiders.py` proves the analysis output is **identical**
before and after a disclosure ingestion.

### What the live run selected

| | |
|---|---|
| As-of | 2026-09-17, cutoff `2026-09-18T04:00:00+00:00` |
| 10-K | `0001045810-26-000021` (accepted 2026-02-25) |
| 10-Q | `0001045810-26-000075` (accepted 2026-08-26) |
| 8-Ks | `0001045810-26-000078`, `…-26-000073`, `…-26-000069` |
| Amendments | **none in the window** — so the warning path is fixture-only |
| Documents | **8** — 5 primary, 3 exhibits, all extracted |
| Facts | **26** observations, 5 concepts matched, 1 unmatched |
| Requests | 12 of 24 |

The unmatched concept is `revenue_contract_with_customer`: the tag exists in NVDA's history
but the selected 10-K and 10-Q do not use it. It is reported as unmatched rather than as zero.

### How Step 6 will use this

The extracted text is stored per document, with its source content and hash, and the sections
are recorded as offsets into it. That is everything an indexer needs: text to chunk, a stable
document identity to key on, and offsets to attribute a chunk to a section.

The `extraction_version` column is how rows processed by an older extractor are found, since
a re-run does not rewrite derived text.

## Searching filing text (Qdrant)

The filings stored by Step 5B are readable but not searchable. This step chunks them, embeds
each passage locally, and puts the vectors in Qdrant so a question can find the passages that
speak to it.

**This retrieves passages. It does not answer questions, and it does not compute anything.**

```bash
docker compose up -d qdrant
docker compose exec backend python -m app.index_filings --symbol NVDA --dry-run
docker compose exec backend python -m app.index_filings --symbol NVDA
docker compose exec backend python -m app.search_filings --symbol NVDA \
  --as-of 2026-09-17 --query "What export restrictions does the company disclose?" --top-k 5
```

### The model, and the first download

`BAAI/bge-small-en-v1.5` through FastEmbed on the CPU: 384 dimensions, 512 tokens, cosine.
No API key, and no network at query time.

The first indexing run downloads about 130 MB of weights. They go to a **named volume**, not to
`/tmp` — FastEmbed's own default cache is `{tempdir}/fastembed_cache`, which is neither shared
with the host nor kept when the container is recreated, so without the volume every
`docker compose up --build` would download them again. `FASTEMBED_CACHE_PATH` and the
`model_cache` volume are what prevent that.

**Nothing is downloaded at API startup.** The model and the Qdrant client are both created on
first use, so `/health`, `/portfolio` and the analysis commands work whether or not Qdrant is
running — which is also why the `backend` service has no `depends_on` on it.

Qdrant's port is bound to `127.0.0.1:6333`, so it can be inspected from the Mac at
<http://localhost:6333/dashboard> without being exposed to the network.

### Chunking

Text is split at paragraph boundaries first, then at table rows, then at sentences, and only
hard-split as a last resort. The target is 350–450 tokens and the ceiling is 512, **measured
with the model's own tokenizer on the complete embedding input** — heading and special tokens
included.

> The counter deserves a note. FastEmbed configures its tokenizer to truncate at 512, so asking
> *it* how long a 900-token passage is returns `512` — a plausible number that would make the
> limit unenforceable. `app.embeddings` therefore builds a second tokenizer from the same file
> with truncation switched off, and that is what the chunker measures against.

Each passage records its exact character offsets into the extracted text, and its section where
one was detected. The heading added for embedding is bracketed — `[10-K | section:
risk_factors]` — so it can never be read as something the filing said. Sections a filing does
not have stay `unknown`.

### The manifest, and why the index is not the source of truth

`document_index_manifest` records which documents are **completely** indexed and at what
identity. A row is written only after every point for that document has been acknowledged, so
an interrupted run leaves points in Qdrant and no row — which reads as "not indexed", the safe
direction to be wrong in. A retry re-upserts the same deterministic point ids and converges.

> **PostgreSQL and Qdrant are not one transaction, and the code does not pretend otherwise.**
> The manifest is the record of what completed. The failure mode is "indexed but not recorded",
> which a retry fixes — never "recorded but not indexed".

Running it again with unchanged text embeds nothing and adds no points: the manifest matches,
so the document is counted *unchanged*. On the stored sample that is the difference between 38
seconds and 0.04 seconds.

### When the text changes

If a document's content, extraction, chunking or the model changes under an existing index, the
run **stops** and names the document, rather than re-indexing silently or serving passages whose
vectors no longer describe their text.

**Recovery is manual, and deliberately so** — this milestone has no refresh path:

```bash
curl -X DELETE localhost:6333/collections/sec_filings     # drop the derived index
docker compose exec backend python -m app.index_filings --symbol NVDA
```

Qdrant is derived and can always be rebuilt from PostgreSQL, which stays authoritative. Nothing
here ever drops a collection on its own.

### Retrieval

Filters are applied **inside Qdrant, before results are chosen**: the company, and
`acceptance < cutoff`. The cutoff is the same end-of-day America/New_York instant the insider
analysis uses, and it is built from the **acceptance timestamp** and nothing else — a
reporting-period date says which quarter a filing covers, not when the market could read it. A
filing with no acceptance timestamp cannot satisfy the filter; it is excluded and reported.

Every hit is then validated against the manifest, and anything whose document is incomplete or
stale is dropped. The query asks for more than were wanted, so dropping a hit cannot starve the
result.

`--as-of` is **required** and has no default. Every other default in this project stands in for
"the stored range" or "today", but this one changes which filings are visible, and guessing it
would silently change the answer.

The typed result distinguishes seven outcomes, because collapsing any two would report something
untrue:

| Status | Meaning |
|---|---|
| `ok` | passages were retrieved |
| `unknown_company` | no company is stored under that symbol |
| `no_indexed_documents` | nothing has been indexed for it |
| `no_eligible_documents` | nothing indexed was public before the cutoff |
| `no_matching_results` | nothing survived validation |
| `index_unavailable` | Qdrant could not be reached |
| `model_unavailable` | the embedding model could not be used |

Exit status is `0` for every answer *about the data*, including "nothing matched". It is `1`
only when the search could not run at all.

### What retrieval does not do

- **A returned passage is not an answered question.** Every successful result says so.
- **Similarity is not confidence.** It is a cosine similarity, reported as a number and never as
  a percentage or a probability.
- **It cannot tell that a question is unanswerable.** Measured: two questions whose answers are
  not in the stored filings each returned five passages anyway, with scores that look no
  different from the scores on a good answer.
- **It computes no financial metric.** Company Facts and Form 4 transactions are not embedded,
  and nothing here derives a ratio or fills a missing value.

### Measured on the stored NVDA documents

| | |
|---|---|
| Documents indexed | **8** — a 10-K, a 10-Q, three 8-Ks and three exhibits |
| Chunks | **350** |
| First indexing | **38 s**, including the model download |
| Repeat indexing | **0.04 s** — 8 unchanged, 0 embedded again, 0 new points |
| Retrieval latency | ~0.02 s per query once the model is loaded |
| Qdrant restart | 350 points still present, search unaffected |

The collection now holds a second company — AAPL's 4 documents and 193 chunks went in beside
NVDA's 350 points without changing any of them (see *Adding a company other than NVIDIA*).
Company is the first filter Qdrant applies, so a search for one issuer cannot return the
other's filings.

**Retrieval evaluation**, with the expected evidence chosen before searching:

| | |
|---|---|
| Answerable questions | **8 of 8 retrieved supporting evidence in the top 5** — all eight at rank 1 |
| Unanswerable questions | 2 of 2 returned five passages anyway |

Representative citations: *"What export controls does the company disclose?"* → 10-Q
`0001045810-26-000075`, `risk_factors`, similarity 0.749. *"What charge did the company take
related to H20?"* → 10-K `0001045810-26-000021`, `risk_factors`, 0.785.

Those eight questions are a small, hand-picked set drawn from text already known to be present.
They measure whether retrieval finds what is there, not whether it would find what a real user
asks.


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
- **The market and SEC tables are write-only so far.** Ingestion fills them; no endpoint,
  tool, or calculation reads them yet. Everything they contain is raw provider data — no
  derived column exists anywhere.
- **Coverage is US equities with one primary listing per company.** That is why
  `companies.ticker` is unique on its own. A company listed on two exchanges at once
  cannot be represented, and neither can a non-US listing.
- **Two price sources will coexist for a while.** `GET /portfolio/valuation` still values
  the demo portfolio from the hardcoded `DEMO_PRICES` table and still reports
  `"price_source": "demo"`, while `daily_prices` will hold real Twelve Data bars. They are
  separate systems on purpose and nothing reconciles them — the demo path is left working
  so the existing pages keep rendering. `price_source` is how the two stay distinguishable,
  and it should be believed.
- **Nothing matches amendments to originals yet.** `sec_filings.is_amendment` and
  `amends_filing_id` are columns waiting for a later step. Until something populates them,
  a Form 4 and its `4/A` are two unrelated filings, and counting both would double-count
  the transactions they share. That is a known gap, not a subtlety to be discovered later.
- **No derived columns exist, deliberately.** There is no sentiment, divergence,
  recommendation, or confidence field anywhere in the schema. Those are Module 1's output,
  and they belong to the step that computes them from evidence rather than to the tables
  that store the evidence.
- **Imported prices are stored at whatever precision the provider gave.** The columns are
  unconstrained `NUMERIC`, so a seven-decimal price is stored with all seven digits. This is
  the one place in the schema where a numeric column has no declared scale, and it is
  deliberate: the alternative was rounding real data on the way in. See "Precision: nothing
  is rounded, anywhere".
- **Volume adjustment is unverified.** Prices are requested split-adjusted. Whether the
  provider adjusts volume under that same mode has not been established, so a volume should
  be treated as reported until something proves otherwise.
- **The price client reaches one provider and one interval.** Twelve Data only, `1day` only,
  one symbol per call. No pagination, no date-range paging, no retries, and no second
  provider. `daily_prices.provider` exists so a second one can be added without a migration,
  not because one is planned yet.
- **EDGAR discovery stops at the recent list.** 1001 filings for NVIDIA, with 1478 older ones
  from 1998–2020 in separate submission files that are not fetched. A search that claims to
  cover a company's insider history does not, yet.
- **Only Form 4 is read.** Not Forms 3 or 5, not Schedule 13D/G, not prospectuses. Form 4 is
  the one that reports transactions as they happen.
- **A filing's owners are never attributed to individual transactions.** Form 4 does not
  carry that, so a joint filing's transactions belong to all its owners. Anything that
  attributes a trade to one named insider is guessing.
- **The SEC client makes no attempt to work around a block.** No retries, no proxy rotation,
  no browser impersonation. A 403 or a 429 stops the run and says so.
- **Filing data is stored raw, and nothing is derived from it.** No divergence, no sentiment,
  no recommendation, no buy/sell classification. `insider_transactions.transaction_code`
  keeps EDGAR's own letters — `A`, `S`, `G`, `M`, `F` — and interpreting them belongs to the
  step that does it deliberately.
- **In ingestion, only identity is general — the bounds are not.** Any US-listed issuer can be
  named on the command line, but the limits are the same small ones NVDA was fetched under:
  30 bars, 3 Form 4 filings, one 10-K and one 10-Q. A second company is now stored, not
  covered.
- **There is no refresh policy for changed data.** An ingestion conflict stops the run and
  names the record. Provider corrections, revised split history, and re-parsing after a
  parser fix all land there, and each needs a deliberate answer before this can be scheduled
  rather than run by hand.
- **The analysis is a description, not a signal.** `price_up_net_selling` says the price rose
  while the transactions in *these filings* netted to selling. It is not a prediction, not a
  rating, and not a reason to trade. Nothing in the project produces a recommendation.
- **Its universe is three filings and thirty bars.** Every result carries
  `insufficient_coverage`, and that is not boilerplate — it is the correct description of what
  a bounded sample supports.
- **Portfolio exposure is still not calculated.** NVDA and AAPL now have real prices and
  filings stored, but MSFT does not, and no endpoint reads the ingested bars — `GET
  /portfolio`, `GET /portfolio/valuation` and `POST /portfolio/scenario` still return the
  seeded demo prices, byte for byte as before. Checked after adding AAPL.
- **Amendment blocking is broader than it needs to be.** One `4/A` before the cutoff withholds
  the whole divergence comparison, because working out which rows it corrects is a separate
  problem. Narrowing that is a deliberate future change, not an oversight. A `10-K/A` does
  **not** block it — that is tested, because an amendment to a disclosure filing says nothing
  about insider transactions.
- **Disclosure coverage is a window, not a history.** One 10-K, one 10-Q and the three most
  recent 8-Ks from 90 days. Older filings are not paged through, and the three 8-Ks are the
  most recent ones rather than the material ones — nothing here knows which events mattered.
- **Section extraction is good, not perfect.** The contents-page cluster is detected and
  removed, and all four 10-K sections land correctly on the stored filing. Positions are
  offsets, not verified spans, and a section that cannot be found is reported as not detected
  rather than guessed at.
- **Company Facts is fetched today, not as of the analysis date.** What is stored is a
  current-source snapshot filtered to the selected filings. It is not a reconstruction of what
  the endpoint returned on the as-of date, and it is labelled as such in the output.
- **Two revenue concepts exist and are never combined.** `Revenues` and
  `RevenueFromContractWithCustomerExcludingAssessedTax` are different tags with different
  definitions. A revenue series built from both would be neither.
- **The search index is derived and disposable.** Qdrant can be dropped at any time and
  rebuilt from PostgreSQL with `python -m app.index_filings`. Nothing in it is a source of
  truth, and no fact exists only there.
- **A changed document stops indexing rather than refreshing it.** The run names the document
  and refuses. Recovery is a manual collection delete followed by a re-index, and at 15–20
  companies that will be too blunt to keep — a per-document refresh is the obvious next step.
- **Retrieval returns passages, never answers.** Vector search cannot tell that a question is
  unanswerable; measured, it returns five confident-looking passages for questions the filings
  do not address. Anything built on top has to carry that caveat forward.
- **The eight evaluated questions were chosen from text already known to be present.** They
  measure whether retrieval finds what is there, not how it would fare on a real user's
  question. That is a floor, not a benchmark.
- **Chunking is deliberately unsummarised.** Passages are split at paragraph, table-row and
  sentence boundaries and embedded as written. Nothing rewrites or condenses the filing's own
  words, which is what makes a citation checkable.
- **Re-extraction has no path yet.** The extraction is derived from the document content and is
  deliberately not part of its identity, so improving the extractor does not make a re-run
  conflict — and does not rewrite anything either. `extraction_version` is how such rows would
  be found; performing the refresh is a later step.

## Next milestone

**Module 1, step 7: typed tools and the Module 1 agent.** Everything the agent needs is now
stored: prices, insider transactions, a deterministic analysis over them, filing text in
PostgreSQL, and a searchable index over that text. Nothing yet puts them behind tools, and no
model reads any of it.

Four things step 7 inherits, all deliberate:

- **A financial tool must return an explicit "unavailable"**, with a reason, rather than
  leaving every caller to consult a warning list. Company Facts reports unmatched concepts;
  that is a fact about the data, and turning `sum([])` into `0` downstream is the mistake to
  avoid.
- **A financial tool must pick its period and accession**, not sum every observation of a
  concept. A quarter and a year-to-date figure can share an end date and a fiscal period.
- **Retrieved passages are evidence, not answers.** Two questions with no answer in the stored
  filings each returned five passages anyway.
- **`insufficient_coverage` remains the honest conclusion** for anything drawn from insider
  activity, because three filings are a sample.

The remaining Module 1 steps, in order: supervisor routing, `POST /analysis/chat`, and the
Analysis chat UI.
