# Payments Analytics Copilot

A natural-language analytics assistant over payments data: ask a business
question, get back an executed SQL query, a chart, a grounded explanation,
and explicit caveats — not a hallucinated summary.

> Built for the "Payments Analytics Copilot" take-home challenge. This is a
> single coherent vertical slice, not a production system — see
> **Assumptions & limitations** below.

## What it does

1. You ask a question ("Why did success rate decline recently?", "Revenue
   by region in the last 30 days?") and pick a role (Admin / Analyst /
   Viewer).
2. The question is parsed into an **intent** (metric, dimension, time
   window) and turned into SQL by a schema-constrained templater. If the
   Claude wrapper is configured (`WRAPPER_ENDPOINT`/`WRAPPER_API_KEY`),
   Claude instead *drafts* the SQL — but that draft is never trusted
   directly: it is always re-validated before execution, and falls back to
   the safe templater if it fails validation.
3. The SQL passes a **validator**: SELECT-only, single-statement, table/
   column allow-list, role-based access control (e.g. an Analyst cannot
   query `bank_accounts`), and an automatic row `LIMIT` / execution-step
   budget.
4. The query runs against a local SQLite database of synthetic payments
   data, with a **data-quality check** (missing fields, duplicated
   records) run alongside it.
5. "Why did X change" questions get a **mix-shift decomposition**: the
   change in the metric is split into how much is explained by a shift in
   transaction *mix* across payment-method/region segments vs. a genuine
   change in *success rate within* those segments — so the answer doesn't
   jump to a single spurious cause.
6. The result is rendered as a chart + table, with the generated SQL shown
   verbatim, facts kept separate from hypotheses/recommendations, and all
   caveats (assumptions made, data-quality issues, access restrictions)
   listed explicitly.

## Requirements covered

- **Two related datasets**: `transactions` + `merchants` (+ `bank_accounts`,
  `settlements`) — see `backend/schema.py`.
- **Natural-language questions about metrics/trends**: `backend/nl2sql.py`.
- **Generate & execute SQL**: templater (always on) + optional LLM draft,
  both executed against real SQLite — `backend/nl2sql.py`,
  `backend/analysis.py`.
- **Show the query/analysis steps used**: "Generated Query" + numbered
  steps panel in the UI.
- **Charts/tables for key findings**: Chart.js bar chart + raw table.
- **Cite dataset, filters, time window**: "Caveats & Citations" panel.
- **Distinguish facts from hypotheses/recommendations**: separate panels,
  built in `backend/narrative.py` from computed numbers only (never from
  free-text LLM generation of the numbers themselves).
- **Role-based access control**: `backend/schema.py` + `backend/validator.py`
  — try the "Show me bank account numbers" example as Analyst vs Admin.
- **Data-quality warnings**: seeded NULL amounts + duplicated
  `external_ref`s, surfaced by `analysis.data_quality_report`.
- **Query cost/timeout controls and caching**: enforced row `LIMIT` +
  SQLite progress-handler step budget (`analysis.run_query`), plus a
  120-second in-process response cache keyed by (question, role) —
  `backend/cache.py` — so a repeated question makes no SQL/LLM call at all
  (the UI labels cached answers "[served from cache]").
- **Compare cohorts / drill-down**: an explicit "compare A vs B" question
  (e.g. "Compare Card vs Wallet success rate") is detected and restricted
  to exactly those two values, rather than a full breakdown —
  `nl2sql._detect_cohort`. The "why did X decline" prior-vs-recent
  decomposition is the time-based form of the same idea.
- **Confidence/uncertainty explanations**: `analysis.confidence_notes`
  reports a 95%-CI margin of error for success-rate estimates (normal
  approximation on the underlying sample size) and flags any segment with
  fewer than 200 transactions as low-confidence, shown in their own
  "Confidence & Uncertainty" panel.
- **Evaluated against known-answer test questions**: `tests/test_known_answers.py`
  (run with `pytest`) checks the generated-SQL pipeline's output against
  independently hand-written "golden" SQL for the same questions, checks
  the mix-shift decomposition is internally consistent, and checks the
  validator's safety/access-control properties (blocks writes, blocks
  multi-statement SQL, blocks restricted tables for non-admin roles,
  allows CTEs, injects a LIMIT). 11 tests, all passing.
- **Failure scenarios demonstrated**:
  - *Missing/duplicated records* → data-quality caveats on every query.
  - *Access-scope violation* → Analyst/Viewer asking for bank account
    numbers gets a 403 with the attempted SQL shown, not silently denied.
  - *Two sources disagree on the same metric* → `analysis.cross_source_revenue_check`
    compares the transactions ledger's revenue against the settlements
    ledger's settled amount for the same window (they legitimately diverge
    due to settlement lag) and surfaces the discrepancy with guidance on
    which figure to use for which purpose, rather than silently picking one.
  - *Ambiguous time period* → defaults to 30 days with an explicit
    "Assumption:" caveat.
  - *Success-rate decline explained by mix shift* → the decomposition
    panel for "why did success rate decline" questions.
  - *Unsafe/expensive generated query* → an LLM-drafted query that fails
    validation (e.g. references a restricted table, or isn't a plain
    SELECT/CTE) is rejected and logged in the steps panel, with a safe
    fallback query used instead; a runaway query is aborted by the step
    budget. Verified live against the real wrapper, not just unit-tested.

## Running it

```powershell
pip install -r requirements.txt
python -m backend.generate_data
python -m backend.app
```

Then open `http://127.0.0.1:5057`.

Optional: set `WRAPPER_ENDPOINT`, `WRAPPER_API_KEY`, `WRAPPER_MODEL` in a
local `.env` (not committed) to turn on real LLM-drafted SQL via a Claude
API wrapper — the app works fully without it, using the deterministic
templater. When enabled, Claude's draft is *always* re-validated before
execution and falls back to the safe templater if it fails validation
(e.g. referencing a restricted table, or not being a plain SELECT/CTE).

## Try these questions

- "Why did success rate decline recently?" (mix-shift decomposition)
- "What is the revenue by region in the last 30 days?" (also triggers the
  transactions-vs-settlements disagreement caveat)
- "Success rate by payment method last 14 days" (confidence/uncertainty panel)
- "Compare Card vs Wallet success rate" (cohort drill-down)
- "Show me bank account numbers for all merchants" — as **Business
  Analyst** (blocked) then as **Admin** (allowed)

## Testing

```powershell
pip install -r requirements.txt
python -m backend.generate_data
pytest
```

## Assumptions, mocked dependencies & limitations

- **Data is synthetic**, generated by `backend/generate_data.py` with a
  fixed seed. It deliberately encodes a mix-shift (Wallet payments in the
  East region grow in share and have a structurally lower success rate)
  and data-quality defects (missing amounts, duplicated records) so the
  failure scenarios are reproducible.
- **NL understanding is a lightweight keyword/intent parser**, not a full
  text-to-SQL model, when no Claude wrapper is configured. It covers
  the metrics/dimensions/time-windows used in the example questions
  reliably; open-ended questions outside that vocabulary will fall back to
  a best-effort default rather than genuinely general NL understanding.
  The LLM path (when enabled) is more general but is always re-validated,
  never trusted blindly.
- **Role-based access control is table/column-level**, not row-level
  (e.g. there's no "only your own merchant's data" scoping).
- **No durable workflow engine, queue, or persistent history** — each
  question is answered synchronously and independently; there's no saved
  query history or multi-turn conversation memory.
- **Query cost control is a step-count heuristic** (SQLite's
  `set_progress_handler`), not a real query planner cost estimate or
  wall-clock timeout.
- **Single-user, single-process, no auth** beyond the role dropdown — this
  is a demo of the *reasoning and safety* pattern, not a deployable
  multi-tenant service.
