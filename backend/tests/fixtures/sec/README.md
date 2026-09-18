# SEC Form 4 fixtures

One file here is a **real SEC filing**. The rest are **synthetic** — written by hand to
cover cases real NVDA filings did not happen to contain. Each synthetic file says so in a
comment at the top, so a reader never has to guess which is which.

Nothing in this directory is used as evidence that the live SEC interfaces work. A fixture
proves the parser handles a shape; only a real fetch proves the fetch works.

## Real

### `real_nvda_form4_0002152188-26-000005.xml`

| | |
|---|---|
| Source | <https://www.sec.gov/Archives/edgar/data/1045810/000215218826000005/wk-form4_1789160684.xml> |
| Accession | `0002152188-26-000005` |
| Form | `4` (not an amendment) |
| Issuer | NVIDIA CORP, CIK `0001045810`, ticker `NVDA` |
| Filed | 2026-09-11, accepted 2026-09-11T21:04:47Z |
| Period of report | 2026-09-09 |
| Reporting owner | Parker Nicholas P., CIK `0002152188`, officer, "EVP, Worldwide Field Ops" |
| Transactions | 1 non-derivative (code `A`, 172,507 shares, price **0**) |
| Footnotes | F1 (vesting schedule), F2 (RSUs received for no consideration) |

Downloaded verbatim; the bytes are unmodified.

It earns its place for three reasons the synthetic files cannot cover. It is a genuine
`xslF345X06/wk-form4_*.xml` archive path, so it pins what the client has to strip. Its
price is an **explicitly reported zero** carrying a footnote, which is exactly the case
that must not collapse into "missing". And its footnote references sit inside
`<transactionShares>` and `<transactionPricePerShare>` respectively, so field-level
attribution is tested against real markup rather than against markup written to agree with
the parser.

## Synthetic

All hand-written. None of these are SEC filings, and none should ever be quoted as
evidence about a real company.

| File | What it exercises |
|---|---|
| `synthetic_multiple_owners_one_transaction.xml` | Two reporting owners on one filing with a single transaction. The owners must stay two, and the transaction must stay one — the fan-out that Module 1's calculations must not fall into. Also an indirect ownership with `natureOfOwnership`. |
| `synthetic_derivative_and_nonderivative.xml` | Both source tables in one document; derivative-only fields (underlying security, exercise price, expiration); a `nonDerivativeHolding` **and** a `derivativeHolding` that must not become transactions; an option exercise at price 0. |
| `synthetic_namespaced.xml` | A valid ownership document in a default XML namespace. Real filings have none; the parser matches on local names so both work. Also a seven-decimal price, which `numeric(18,6)` cannot hold. |
| `synthetic_amendment.xml` | `documentType` of `4/A`, so `is_amendment` derives `True`, plus `<dateOfOriginalSubmission>`, which appears only on amendments. Says nothing about which accession it amends, because the document does not. |
| `synthetic_zero_transactions.xml` | Owners but no trades at all. Must parse successfully with an empty transaction tuple. |
| `synthetic_malformed_number.xml` | A share count of `not-a-number`. The whole document must be refused, not silently stripped of the row. |
| `synthetic_external_entity.xml` | A `DOCTYPE` declaring an external entity pointing at `file:///etc/passwd`. Must raise without reading anything. |

The synthetic derivative fixture uses `<conversionOrExercisePrice>` — the name in schema
X0609. The parser also accepts `<exercisePrice>`, because the element has been named more
than one way across schema versions and no real derivative filing was available to settle
which is current.

## Reproducing the real fixture

```bash
docker compose exec backend python -m app.fetch_sec --limit 1
```

The `source_xml_url` in that output is the file to fetch. The accession number is recorded
above so the exact filing can be found again from SEC's own archive.