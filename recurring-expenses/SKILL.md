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
  nothing to do. If a caller still wants the latest, fold in only what changed
  since `Last refreshed:` with one narrow `query` (`report: "reg"`,
  `account_pattern: "Expenses"`, `begin_date: <last refreshed date>`,
  `output_format: "csv"`) - new charges, price changes, series that went
  quiet - and rewrite the file.
- **Stale, missing, or forced** - fingerprint mismatch, older than 14 days,
  absent, or the user asked for a refresh: run the full detection below and
  rewrite the file. A fingerprint mismatch means the ledger changed under the
  cache (a backfill, a payee rename, an account cleanup), so redo the whole
  detection - no narrow top-up.

## Detecting recurring charges

If the journal already encodes recurrence - an account hierarchy like
`Expenses:Subscriptions:*` or `Expenses:Rent`, or tags on postings - trust
that structure and use detection only to fill the gaps.

1. Pull the last 13 months of expense postings with `query`: `report: "reg"`,
   `account_pattern: "Expenses"`, `begin_date: <13 months ago>`,
   `output_format: "csv"`. 13 months, not 12, so an annual payment appears
   twice. For large ledgers, narrow follow-up queries with `payee_pattern`
   instead of re-pulling.
2. Group postings by payee. A payee is a recurring-charge candidate when all
   hold:
   - it appears in **3 or more distinct months** (or twice, ~1 year apart, for
     annual payments), and
   - the interval between charges is regular - monthly +/-4 days, weekly
     +/-1 day, quarterly +/-1 week, yearly +/-2 weeks, and
   - the amounts are identical, step between two stable values (a step is a
     price change, not a disqualifier - record it), or fluctuate within a
     stable band (a metered utility: same payee every month, varying amount).
     Report a banded amount as a range, e.g. "~180-250".
3. Regularity beats frequency: payees with irregular intervals (groceries,
   restaurants, fuel - bought when needed, not on a schedule) are not
   recurring, no matter how often they appear.
4. One payee can hide both a recurring charge and ordinary shopping (Amazon
   orders vs Prime, an Apple device vs Apple Music). Isolate the regular
   series and judge it alone - never flag a payee wholesale.
5. Payee spelling drifts ("Netflix" vs "NETFLIX.COM" vs "Netflix.com
   Amsterdam") - normalize case and punctuation when grouping, and note which
   payees you merged so the user can correct you.
6. Count each real-world charge once: a PayPal-wrapped payment plus the
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
Currency: EUR

## Bills and fixed obligations

| Payee | Account | Cadence | Amount | Approx monthly | First charged | Last charged | Next expected | Notes |
|---|---|---|---|---|---|---|---|---|
| Landlord | Expenses:Rent | monthly | 1450 | 1450 | 2024-01-01 | 2026-08-01 | 2026-09-01 | |
| City Utilities | Expenses:Utilities | monthly | ~90-140 | 115 | 2024-01-15 | 2026-08-14 | 2026-09-14 | amount varies |

## Subscriptions and memberships

| Payee | Account | Cadence | Amount | Approx monthly | First charged | Last charged | Next expected | Notes |
|---|---|---|---|---|---|---|---|---|
| Netflix | Expenses:Subscriptions | monthly | 12.99 | 12.99 | 2024-03-11 | 2026-08-11 | 2026-09-11 | merged from 3 spellings |

## Totals

- Per month: 1738 EUR
- Per year: 20856 EUR
(If several currencies appear, one line per currency. Never convert.)

## Price increases

- Netflix: 11.99 -> 12.99 on 2026-02-11 (+8.3%, +12/yr)

## Expected but not seen

- Old Gym: monthly, last charged 2026-04-02, ~2 cadences overdue - stopped, switched, or cancelled
```

Column notes:

- **Payee** - the normalized name (spelling variants merged).
- **Account** - the full expense account, e.g. `Expenses:Utilities`.
- **Amount** - a single figure, a `A -> B` step for a price change, or a
  `~low-high` range for a metered bill.
- **Approx monthly** - the amount normalized to a month (annual / 12,
  quarterly / 3, weekly x 52 / 12); the average for a banded bill.
- **First charged** - earliest charge date seen in the 13-month window; treat
  it as "at least since" for anything older than the window.
- **Next expected** - last charged + cadence.
- **Notes** - short flags only ("amount varies", "merged from 3 spellings");
  empty when there is nothing to say.
- **Price increases** and **Expected but not seen** - fill these in even when
  empty (keep the heading with "- none"), because consumers read them
  directly rather than re-deriving.

## Memory pointer

Once (only if it is not already there), record a one-line pointer in memory
the same way you store any durable fact: recurring bills and subscriptions are
cached in `recurring-expenses.md`, rebuilt by this skill and read by `budget`,
`weekly-recap`, `recurring-spending`, and `subscription-audit`; the file's own
`Last refreshed` line is the source of truth for freshness. Do not touch
memory on later refreshes.

## Boundaries

- Read-only on the journal. The only file this skill writes is
  `recurring-expenses.md` - never edit, validate, or commit journal entries.
- Not a report. If the user wants to see their recurring spending, hand off to
  `recurring-spending` or `subscription-audit` after the cache is built.
- If the ledger covers less than ~3 months, say the history is too short to
  detect recurrence and write no cache. Annual patterns need more than a year
  of history - note that in the file when the ledger is younger than that.
