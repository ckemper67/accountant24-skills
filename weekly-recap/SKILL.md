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
- Report the current net worth, the absolute and percentage change over the
  week, and which currency it's in. If several currencies appear, report each
  separately - do not convert unless the user asks.
- If a single account or category drove most of the change (e.g. a large
  transfer, a market move on an investment account), call it out by name.

## 2. Spending breakdown by category

- `report: "bal"`, `account_pattern: "Expenses"`, `begin_date: <7 days ago>`,
  `end_date: tomorrow`, `depth: 2`, `output_format: "csv"`.
- Present a table of categories sorted by amount, largest first, with each
  category's share of the week's total spending as a percentage.
- State the week's total spend and, if income postings exist in the same
  window, whether spending stayed within income.
- Compare against the trailing 4-week average per category (same query with
  `begin_date: <28 days ago>`, dividing by 4) and flag any category that's
  meaningfully above its average (roughly 50% or more) as standing out this
  week. Skip this comparison silently if there's under a month of history.

## 3. Recurring payments expected in the week ahead

The journal has no native scheduled-transaction record. The `recurring-expenses`
skill reconstructs one from history and caches it at the workspace root in
`recurring-expenses.md`. Read that cache - do not re-derive recurrence here.

1. Read `recurring-expenses.md`. If it does not exist, run the
   `recurring-expenses` skill once to build it, then continue from the file.
2. Check its `Last refreshed:` line. If it is more than 14 days old, say once,
   briefly, that the projection is approximate; still use the cached tables
   rather than re-scanning 13 months of history - refreshing the cache is the
   `recurring-expenses` skill's job, not this one's.
3. Take every row from both tables (bills and subscriptions). Use each row's
   `Next expected` date; if a row has none, project it as `Last charged` +
   cadence. Keep only the rows whose next date falls within the next 7 days.
4. Also fold in the cache's **Expected but not seen** list: any overdue item
   whose next projected date is within the window is still "coming up" - flag
   it as already overdue.
5. List what is left ordered by date: payee, expected date, expected amount
   (a range if the row's amount varies), and account.

If nothing recurring is expected in the next 7 days, say so plainly instead of
stretching older history to fill the section.

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
