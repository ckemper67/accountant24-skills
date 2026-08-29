---
name: budget
description: Builds a monthly and yearly spending budget per account from your last 13 months of expenses, writes it to budget.md, and refines it with your input. Ask things like "set up a budget", "create a budget for me", or "update my budget". Once budget.md exists, future runs revise it in place instead of starting over.
---

# Budget

Build a per-account monthly and yearly budget from the user's spending
history, write it to `budget.md`, and refine it with the user before treating
it as final. This file becomes the reference a future comparison ("am I on
budget?") reads against, so get its shape right — every account name must
match the ledger exactly, and every number must be traceable to real data.

Building the proposal is **read-only**: use the `query` tool to read the
ledger, and the bundled `compute_budget.py` script (via `bash`) only to
aggregate that output into per-account monthly figures — never edit,
validate, or commit the journal while building or revising a budget.

## Pulling the data

1. Check whether `budget.md` already exists in the workspace root. If it
   does, follow "Updating an existing budget" below instead of starting from
   scratch.
2. Pull the last 13 months of expense postings, grouped by month, with the
   `query` tool: `report: "bal"`, `account_pattern: "Expenses"`,
   `period: "monthly"`, `begin_date: <13 months ago>`, `depth: 2` or `3`
   (deep enough for meaningful categories — Groceries, not just Expenses;
   shallow enough to stay readable — usually not the full account path),
   `output_format: "csv"`, `invert: true` (so expenses read as positive
   spend, not the ledger's native negative sign). This returns one row per
   account with one column per period, the last of which is the current,
   still-in-progress month.
3. Write that CSV to a real OS temp path with `bash` (e.g. `mktemp` plus a
   heredoc) — never under `files/` or anywhere else in the git-tracked
   workspace, it's throwaway scratch data.
4. Run the bundled script with `bash` to aggregate it deterministically:
   `python3 <skill directory>/compute_budget.py <csv path>`. This invocation
   itself tells you `<skill directory>`: right above this text is a line
   reading "References are relative to `<path>`." — that `<path>` is the
   directory this file lives in; use it directly, do not guess an absolute
   path. Do not write ad-hoc bash/python to parse or sum the CSV yourself —
   that is exactly what the script does, and it correctly nets signed
   amounts per currency (so a refund reduces the total instead of inflating
   it) instead of naively summing absolute values.
   - Output: one line per account/currency, tab-separated — account,
     currency, full-month count, total, average, current-month (partial),
     then the 12 full months' values in order. Use the per-month values to
     judge outliers and seasonal patterns yourself; the script only
     aggregates, it never decides what counts as unusual.
   - If `python3` is not on PATH, fall back to aggregating the CSV yourself —
     net each period's signed amount per currency, don't take absolute
     values, and divide by the actual number of full-month columns, not a
     hardcoded 12.
5. 13 months, not 12, so the sample includes the current, still-in-progress
   month for context — but **exclude that partial month from every
   average** (the script already does this; use its `current_month` column
   only to say "so far this month you've spent X against a budget of Y").
6. If the ledger has less than ~6 full months of history, say the sample is
   too small for a reliable budget and ask the user whether to proceed
   anyway rather than silently guessing.

## Computing the proposal

For each account/currency line from the script's output:

- **Cross-check with recurring-spending's cache**, if `recurring-expenses.md`
  exists at the workspace root: any cached payee whose Account matches or
  falls under this line and has Cadence `yearly` or `quarterly` confirms an
  annual/seasonal pattern — use that instead of guessing from the raw
  monthly series for this line. Treat the cache as a hint, not a
  requirement: don't refresh it yourself, and if it's missing or doesn't
  cover this account, fall back to reading the monthly series by eye below.
- **Monthly budget** = the script's average, after excluding one-off
  outlier months (a single large one-time purchase, a rare repair) from your
  own read of the per-month values — recompute the average by hand over the
  remaining months when you exclude one. Say what was excluded and why —
  don't silently drop data.
- **Yearly budget** = monthly x 12, **except** for accounts with a clear
  annual or seasonal pattern (insurance, an annual renewal, holiday
  spending, or confirmed by the cache cross-check above) — for those,
  compute the yearly figure from the script's `total` instead of monthly x
  12, and mark the row's basis as `annual`.
- Round to sensible amounts (nearest 5 or 10 in the ledger's currency) — a
  budget is a target, not a restated average.
- If multiple currencies appear for the same account (the script emits one
  line per currency), keep separate budget rows per currency; never convert
  between them.
- Never invent an account that doesn't appear in the ledger's query results.

## Writing budget.md

Write the file to `budget.md` in the workspace root (same location as
`memory.md`), using this format:

```markdown
# Budget

Currency: <e.g. USD>
Based on: <start date> to <end date> (12 full months)
Last revised: <YYYY-MM-DD>

| Account | Monthly budget | Yearly budget | Basis | Notes |
|---|---|---|---|---|
| Expenses:Groceries | 450 | 5400 | avg | |
| Expenses:Insurance | 80 | 960 | annual | paid yearly in March |
| ... | | | | |
| **Total** | **X** | **Y** | | |
```

- **Account** = the full account name exactly as it appears in the ledger
  (e.g. `Expenses:Groceries`), so a future query can match it directly.
- **Basis** = `avg` (flat 12-month average) or `annual` (computed from a
  yearly total) — a future comparison needs to know which one to check.
- **Notes** = short flags: excluded outlier months, merged categories,
  anything the user should sanity-check. Leave empty when there's nothing to
  note.
- Include a totals row summing the monthly and yearly columns.
- The header (currency, date range, last revised) is required — it's the
  context any future "are we on track" comparison needs.

## Review loop

1. After writing the file, present the table to the user in chat and
   explicitly ask for corrections: amounts to adjust, accounts to merge or
   split, accounts to exclude.
2. Revise `budget.md` in place based on the answer — rewrite the whole file
   each time rather than patching individual lines, and update
   "Last revised" to today's date.
3. Repeat until the user confirms the budget looks right. Don't treat a
   first draft as final without at least one round of confirmation.

## Updating an existing budget

If `budget.md` already exists, don't regenerate it from scratch:

1. Read the existing file.
2. Re-pull the last 13 months as above.
3. Propose deltas — accounts trending meaningfully over or under their
   current budgeted figure — rather than silently overwriting the file.
4. Apply changes the user agrees to, following the same review loop as a
   first-time budget, and update "Last revised".

## Boundaries

- **Read-only.** Use the `query` tool to read the ledger and
  `compute_budget.py` only to aggregate that output — never edit, validate,
  or commit journal entries while building or revising a budget.
- The CSV scratch file (wherever you create it) is throwaway data, not a
  ledger record — never write it under `files/` or anywhere else in the
  git-tracked workspace.
- If the ledger covers less than ~6 months, say the history is too short for
  a reliable budget instead of guessing.
