---
name: recurring-expenses
description: Detects the payments you make on a schedule - rent, utilities, insurance, loans, phone, internet, plus subscriptions and memberships - from the last 13 months of ledger history, and writes them to recurring-expenses.md at the workspace root so other skills can reuse the result without re-deriving it. This is the builder for that cache, not a report - for a readable overview ask for "recurring spending" or a "subscription audit". Run it when recurring-expenses.md is missing or stale, or when asked to "refresh recurring payments".
---

# Recurring Expenses

Reconstruct the user's recurring charges from ledger history and cache them in
`recurring-expenses.md` at the workspace root. hledger has no native record of
a scheduled or automated payment, so this file *is* that record: a register of
the payment series the ledger implies, each with its cadence, typical amount,
account, how far back it goes, and when it is next due.

This is a producer skill. Other skills (`budget`, `weekly-recap`,
`recurring-spending`, `subscription-audit`) read `recurring-expenses.md`
instead of scanning 13 months of history themselves. It is read-only on the
journal - the cache file is the only thing it writes.

## What the data is

Two views of the same set of series:

- **Backward** - what recurs: payee, account, cadence, amount (or a band for
  metered bills), first and last seen, price-change history, and anything that
  was recurring but has stopped appearing.
- **Forward** - what is due next: each series' next expected date and amount.

Consumers use one or both: `weekly-recap` reads the forward view for "due in
the next 7 days"; `budget` reads the backward view to spot annual and
quarterly patterns; `recurring-spending` and `subscription-audit` present the
whole thing.

## When to run

- A consumer skill needs `recurring-expenses.md` and it is missing or stale.
- The user asks to refresh recurring payments, or to rebuild the cache.

Do not run this for a user-facing overview - that is what `recurring-spending`
and `subscription-audit` are for. They call this skill only to (re)build the
cache, then read from it.

## Freshness check (before detecting anything)

If `recurring-expenses.md` already exists, read its first two lines -
`Last refreshed:` and `Ledger fingerprint:`. Then run `query`
`report: "stats"`, `account_pattern: "Expenses"` (plain text - `stats` has no
CSV mode; scoping to Expenses avoids false invalidation from unrelated income
or transfer activity) and compare its `Txns`, `Payees/descriptions`, and
`Accounts` counts against the cached fingerprint.

- **Fresh** - fingerprint matches and refreshed within the last 14 days:
  nothing to do, use the file as it stands.
- **Stale, missing, or forced** - fingerprint mismatch, older than 14 days,
  absent, or the user asked for a refresh: run the full 13-month detection
  below and rewrite the whole file. There is no partial-update path - always
  redo the full detection so `First charged`, cadence, and the price history
  stay derived from the same window.

## Detecting recurring charges

If the journal already encodes recurrence - an account hierarchy like
`Expenses:Subscriptions:*` or `Expenses:Rent`, or tags on postings - trust
that structure and use detection only to fill the gaps.

1. Pull the last 13 months of expense postings with `query`: `report: "reg"`,
   `account_pattern: "Expenses"`, `begin_date: <13 months ago>`,
   `output_format: "csv"`. 13 months, not 12, so an annual payment appears
   twice. Write the CSV to a real OS temp path with `bash` (e.g. `mktemp`) -
   never under `files/` or anywhere in the git-tracked workspace.
2. Run the bundled script to do the mechanical grouping and interval maths
   deterministically: `python3 <skill directory>/detect_recurring.py <csv path>`.
   This invocation itself tells you `<skill directory>`: right above this text
   is a line reading "References are relative to `<path>`." - that `<path>` is
   the directory this file lives in; use it directly, do not guess an absolute
   path. Do not write ad-hoc bash/awk to group the rows yourself - that is
   exactly what the script does (grouping by payee + commodity with
   case/whitespace folding, distinct-month counts, interval sequence, cadence
   guess, regularity score, amount shape, approx-monthly normalization, and an
   overdue flag). It needs only Python's standard library, and handles both
   `1,234.56` and `1.234,56` decimal styles.
   - Output: a `#`-commented header naming the columns, then one tab-separated
     line per candidate payee, sorted by `recurring` guess (`yes`, `weak`,
     `no`), then approx monthly cost DESCENDING, then payee. One payee can
     appear on two rows if it was charged in two commodities - keep them
     separate. A trailing `#` line reports groups dropped for too little
     history and rows dropped while reading; if it exits non-zero with "no
     usable expense postings", the CSV is empty or wrong - do not read that
     as "nothing recurs". Pass `--today YYYY-MM-DD` only to reproduce a past
     run.
   - If `python3` is not on PATH, fall back to doing steps 3-7 by hand from
     the CSV - group by payee, count distinct months, check interval
     regularity - slower and more error-prone, but keeps the skill working.
3. Take the script's `yes` rows as recurring. Review every `weak` row
   yourself: a `banded` shape with high `regularity` and a bills-type account
   (utilities, phone) is a real metered bill - keep it; a two-charge row
   (`postings` = 2, so cadence rests on a single interval) needs a third
   charge or clear real-world knowledge before you trust it; a `weak` row
   that is really irregular shopping - drop it. Ignore `no` rows unless the
   account or payee name clearly says otherwise.
4. Regularity beats frequency: a payee with `cadence: irregular` (groceries,
   restaurants, fuel - bought when needed) is not recurring, no matter how
   many postings it has.
5. One payee can hide both a recurring charge and ordinary shopping (Amazon
   orders vs Prime, an Apple device vs Apple Music). The script groups by
   payee, so a mixed payee shows up as `irregular` or `banded` with a wide
   range - judge the regular series alone, never flag a payee wholesale.
6. Merge spelling variants the script left separate ("Netflix" vs
   "NETFLIX.COM" vs "Netflix.com Amsterdam" - it only folds case and
   whitespace). Sum their postings and note which rows you merged so the user
   can correct you.
7. Count each real-world charge once: a PayPal-wrapped payment plus the
   underlying merchant, or a card settlement plus the expenses it covers, is
   one charge - keep the expense side only.

## Classifying

Sort each recurring charge by one test - could the user cancel it today, with
no penalty, and keep functioning?

- **Bills and fixed obligations** (no): rent or mortgage, utilities,
  insurance, loan payments, taxes, phone plans, internet, childcare.
- **Subscriptions and memberships** (yes): streaming and music, software and
  SaaS, apps, cloud storage, news and magazines, gym and other memberships,
  recurring donations.

`subscription-audit` reads this grouping straight off the cache - keep it
consistent, and never put a cancellable service in the bills table or vice
versa.

## Writing recurring-expenses.md

Write the file at the workspace root (same place as `memory.md` and
`budget.md`), as plain markdown - the reader is another skill or a human, not
a parser, so no format beyond readable tables.

```markdown
# Recurring expenses

Last refreshed: 2026-08-29
Ledger fingerprint: 812 txns, 143 payees, 37 accounts
Currencies: EUR

## Bills and fixed obligations

| Payee | Account | Currency | Cadence | Amount | Approx monthly | First charged | Last charged | Next expected | Notes |
|---|---|---|---|---|---|---|---|---|---|
| Landlord | Expenses:Rent | EUR | monthly | 1450 | 1450 | 2024-01-01 | 2026-08-01 | 2026-09-01 | |
| City Utilities | Expenses:Utilities | EUR | monthly | ~90-140 | 115 | 2024-01-15 | 2026-08-14 | 2026-09-14 | amount varies |

## Subscriptions and memberships

| Payee | Account | Currency | Cadence | Amount | Approx monthly | First charged | Last charged | Next expected | Notes |
|---|---|---|---|---|---|---|---|---|---|
| Netflix | Expenses:Subscriptions | EUR | monthly | 12.99 | 12.99 | 2024-03-11 | 2026-08-11 | 2026-09-11 | merged from 3 spellings |

## Totals

- Per month: 1738 EUR
- Per year: 20856 EUR
(One line per currency. Never convert.)

## Price increases

- Netflix: 11.99 -> 12.99 on 2026-02-11 (+8.3%, +12/yr)

## Expected but not seen

- Old Gym: monthly, last charged 2026-04-02, ~2 cadences overdue - stopped, switched, or cancelled
```

Column notes:

- **Payee** - the normalized name (spelling variants merged).
- **Account** - the full expense account, e.g. `Expenses:Utilities`.
- **Currency** - the commodity the charge is in. The script groups by
  payee + commodity, so a payee billed in two currencies is two rows; keep
  them separate and never merge or convert them.
- **Amount** - a single figure, a `A -> B` step for a price change, or a
  `~low-high` range for a metered bill.
- **Approx monthly** - the amount normalized to a month (annual / 12,
  quarterly / 3, weekly x 52 / 12); the average for a banded bill. Copy the
  script's `approx_monthly`.
- **First charged** - earliest charge date seen in the 13-month window; treat
  it as "at least since" for anything older than the window.
- **Next expected** - last charged + cadence.
- **Notes** - short flags only ("amount varies", "merged from 3 spellings");
  empty when there is nothing to say.
- **Price increases** - only real ongoing series (3+ charges, a `step`
  shape). Do not list a step inferred from a two-charge row - that is as
  likely a one-off as a price change.
- **Expected but not seen** - only rows you classified as recurring whose
  `status` is `overdue`. Keep an irregular/`no` payee out of it.
- **Price increases** and **Expected but not seen** - keep the heading even
  when empty (write "- none"), because consumers read them directly rather
  than re-deriving.

## Memory pointer

Once (only if it is not already there), record a one-line pointer in memory
the same way you store any durable fact: recurring bills and subscriptions are
cached in `recurring-expenses.md`, rebuilt by this skill and read by `budget`,
`weekly-recap`, `recurring-spending`, and `subscription-audit`; the file's own
`Last refreshed` line is the source of truth for freshness. Do not touch
memory on later refreshes.

## Boundaries

- Read-only on the journal. The only file this skill writes in the workspace
  is `recurring-expenses.md` - never edit, validate, or commit journal
  entries. The `reg` CSV is throwaway scratch data: keep it on a real OS temp
  path, never under `files/` or anywhere git-tracked.
- Not a report. If the user wants to see their recurring spending, hand off to
  `recurring-spending` or `subscription-audit` after the cache is built.
- `detect_recurring.py` aggregates; it never decides. Merging real-merchant
  spelling variants, judging a borderline cadence, and classifying bills vs
  subscriptions are yours to do from its output.
- A short history still gets a written cache, never a re-triggering blank.
  The script already detects monthly and weekly series from as little as two
  charges (one interval), returning them as `recurring: weak` - write those.
  Only when the script emits no rows at all (roughly: under ~6 weeks of
  history, or nothing charged twice on a recognizable cadence) write the
  file with just the `Last refreshed:` / `Ledger fingerprint:` lines, a
  `Status: history too short - no series detected` line, and empty tables.
  Tell the user which case applies.
- Annual and quarterly patterns need more than a year of history; when the
  ledger is younger than that, add a `Notes:` line to the file saying they
  may be missed.
