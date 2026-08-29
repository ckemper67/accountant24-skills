---
name: weekly-recap
description: A weekly check-in on your finances, covering three things - how your net worth moved over the last week, what you spent on and in which categories, and what recurring payments are expected in the coming week. Ask things like "give me my weekly recap", "how did my finances do this week", or "what's coming up this week".
---

# Weekly Recap

Give the user a short, three-part status update on the last 7 days and the 7
ahead. This is a read-only analysis: use the `query` tool only - never modify
the journal in this workflow.

## 1. Net worth changes over the last week

Compare total Assets + Liabilities now vs. 7 days ago:

- `report: "bal"`, `account_pattern: "Assets|Liabilities"`, `depth: 0` (flat
  total), `end_date: tomorrow`, `output_format: "csv"` for the current total.
- Same query with `end_date: <7 days ago>` for the prior total.
- Report the current net worth, the absolute change over the week, and which
  currency it's in. Add the percentage change **only when the prior total is
  positive** - if it was zero or negative (liabilities exceeded assets, which
  is normal), a percentage is meaningless, so give the absolute change alone.
  If several currencies appear, report each separately - do not convert
  unless the user asks.
- If a single account or category drove most of the change (e.g. a large
  transfer, a market move on an investment account), call it out by name.

## 2. Spending breakdown by category

- `report: "bal"`, `account_pattern: "Expenses"`, `begin_date: <7 days ago>`,
  `end_date: tomorrow`, `depth: 2`, `output_format: "csv"`.
- Present a table of categories sorted by amount, largest first, with each
  category's share of the week's total spending as a percentage. Compute the
  share **within each currency** - if the week has spending in more than one
  currency, one table (and one total) per currency; never convert or pool
  them. If the week's total is zero, say "no spending this week" and skip the
  table.
- State the week's total spend and, if income postings exist in the same
  window, whether spending stayed within income.
- Compare each category against its **prior**-4-weeks weekly average: same
  query with `begin_date: <35 days ago>` and `end_date: <7 days ago>`
  (28 days = 4 weeks), divided by 4. This baseline must not include the week
  being measured. Flag a category
  as standing out only when it is both roughly 50%+ above that average **and**
  at least ~20 in the ledger's currency (or ~5% of the week's total) - a jump
  from 2 to 4 is noise, not news. Skip this comparison silently if there's
  under ~5 weeks of history.

## 3. Recurring payments expected in the week ahead

The journal has no native scheduled-transaction record. The `recurring-expenses`
skill reconstructs one from history and caches it at the workspace root in
`recurring-expenses.md`. Read that cache - do not re-derive recurrence here.

1. Read `recurring-expenses.md`. If it does not exist, run the
   `recurring-expenses` skill once to build it, then continue from the file.
2. Judge the cache's freshness, lightly - a recap is a quick check-in, not a
   rebuild:
   - Run `query` `report: "stats"`, `account_pattern: "Expenses"` and compare
     its Txns / Payees / Accounts counts against the file's
     `Ledger fingerprint:` line.
   - If the fingerprint mismatches (the ledger changed - often a fresh
     import, which is exactly when a recap runs) or `Last refreshed:` is more
     than 14 days old, say in one line that the projection is approximate and
     offer to refresh it via the `recurring-expenses` skill. Unless the user
     asks for that, still answer from the cached tables - re-deriving
     recurrence is not this skill's job.
3. **Due in the next 7 days.** Take every row from both tables (bills and
   subscriptions). Use each row's `Next expected` date; if it is blank,
   project `Last charged` + cadence. Keep the rows whose date is within the
   next 7 days. List them ordered by date: payee, expected date, expected
   amount (a range if `Amount` varies), currency, and account.
4. **Already overdue.** List every bullet from the cache's
   **Expected but not seen** section as its own flagged group - these are due
   or past due by definition, so do not date-filter them. Each bullet carries
   the payee, cadence, and last-charged date but no amount or account; report
   what is there ("Old Gym - monthly, last seen 2026-04-02, ~2 cycles
   overdue").
5. The producer writes a literal `- none` under
   **Price increases** / **Expected but not seen** when a section is empty -
   treat that as "nothing", never as a payee named "none".

If nothing is due in the next 7 days and nothing is overdue, say so plainly
instead of stretching older history to fill the section.

## Reporting

Structure the recap as three short sections, in the order above, each with a
one-line headline before any table or numbers (e.g. "Net worth is up 320 EUR
this week"). Keep it scannable - this is a quick check-in, not a full audit.
Use the `recurring-spending` or `subscription-audit` skills instead if the
user wants the full recurring-payments picture rather than just the next week.

## Boundaries

- If the ledger covers less than 7 days, say there isn't enough history for a
  weekly recap yet instead of guessing.
- This is a report, not a change to the user's finances - never remove or edit
  journal entries as part of this recap. Building `recurring-expenses.md` when
  it is missing is delegated to the `recurring-expenses` skill; this skill
  only reads it.
