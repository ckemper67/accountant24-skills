---
name: ledger-audit
description: Runs a structural health check over the whole journal and reports what is broken - wrong-sign balances, accounts missing a type, transactions offset to equity:opening-balances instead of a real account, credit-card payments split into disconnected halves, duplicate imports, a mortgage with no interest/principal split, balances that were never reconciled to a statement, historical balances left wrong by a one-off lump correction, gaps in the data, and posting-date vs transaction-date drift. Detects read-only and writes ledger-audit.md, then offers to apply the few fixes that are mechanically safe. Ask things like "audit my ledger", "check my books for problems", "what's wrong with my journal", or "run a ledger health check". For category correctness (which payee belongs in which expense account) use payee-audit instead.
---

# Ledger Audit

Find the structural problems in the user's journal, write them to
`ledger-audit.md` at the workspace root, and - once the user has seen the
report - apply the small set of fixes that are safe to automate. Everything
else is described precisely enough for the user (or a later session) to fix by
hand.

Detecting is **read-only**: run the bundled `audit_ledger.py` script, which
shells out to `hledger` and returns only its findings. Do not edit, validate,
or commit the journal while building the report.

This skill is about *structure* - signs, types, offsets, links, duplicates,
reconciliation, history. It is not about *categories*: whether "Shell" belongs
in `expenses:transport:fuel` is `payee-audit`'s call. When the audit finds
postings with no real category (the `equity-plug` check), it points at
`payee-audit` rather than guessing an account.

## Sign convention

This ledger follows the standard hledger convention: assets and expenses are
positive, income and liabilities and equity are negative. The audit's
`wrong-sign` and `historical-sign` checks are built on that convention. A
credit card can legitimately carry a small positive (credit) balance after an
overpayment, and `equity:opening-balances` has no meaningful sign - the script
already allows for both.

## Running the audit

1. The journal is at `<workspace>/ledger/main.journal`
   (`$ACCOUNTANT24_WORKSPACE/ledger/main.journal`). Confirm it exists.
2. Run the script with `bash`:

   ```
   python3 <skill directory>/audit_ledger.py --journal <workspace>/ledger/main.journal
   ```

   This invocation itself tells you `<skill directory>`: just above this text
   is a line reading "References are relative to `<path>`." - that `<path>` is
   the directory this file lives in. Pass `--today YYYY-MM-DD` only to
   reproduce a past run.
3. The script needs `hledger` on PATH (the same binary the `query` and
   `validate` tools use) and Python's standard library. If it exits non-zero
   with "hledger not found", stop and tell the user - there is no fallback
   that does not run hledger.

### Output

A `#` summary line, then one tab-separated finding per line:

```
severity <TAB> check <TAB> scope <TAB> count <TAB> detail
```

sorted `high` -> `warn` -> `info`, then `#` note lines for things that are
context rather than defects (e.g. "journal uses no secondary dates at all").

- **high** - a number is wrong today. Reconcile or fix before relying on any
  report.
- **warn** - a correctness risk, or history that is wrong even though today is
  right. Worth fixing, not an emergency.
- **info** - hygiene and things that need the user's eyes (a possible
  duplicate, an account with no balance assertion, a card that went dormant).

The script aggregates: one `equity-plug` line per account with a count and a
date range, not one per posting. Trust its grouping; do not re-expand it by
querying every transaction.

## What each check means

| check | what it found | your job |
|---|---|---|
| `strict-check` | `hledger check --strict` fails (unbalanced txn, undeclared account/commodity, failing assertion) | Read the locator in the detail, open that line, report the specific breakage. This blocks clean reporting - surface it first. |
| `ordereddates` | transactions out of date order within one file | Report the file and line; reordering is a manual edit. |
| `uniqueleafnames` | two accounts share a last segment (`assets:bank:checking` vs `assets:other:checking`) | Report both; renaming is the user's call. |
| `missing-type` | a declared account has no `type:` directive | List them. Adding `type:X` lines to `accounts.journal` is a manual edit (see Fixes). |
| `undeclared-account` | an account is used but never declared | Same as `strict-check`'s `accounts` failure; report the names. |
| `wrong-sign` | an account's balance today has the wrong sign for its class | The detail names the cause another check found, or says "unexplained". If unexplained, this account needs a statement reconciliation. |
| `historical-sign` | a balance was the wrong sign at past year-ends | The fingerprint of a correction booked as one late lump entry. Ties to `late-lump-adjustment`. The fix is to distribute the correction across history - manual, and often large. |
| `equity-plug` | postings offset straight to `equity:opening-balances` with no real counterparty | This is a categorisation backlog. Hand the account and date range to `payee-audit`; do not invent target accounts here. |
| `late-lump-adjustment` | a sizeable "balance correction per OFX / adjustment" posting to `equity:opening-balances`, long after the account opened | It squares today's balance and leaves every earlier period wrong. Flag it clearly; recommend distributing it. **Never fix a balance this way yourself** (see Anti-patterns). |
| `broken-chain` | a `bank -> equity` leg and an `equity -> card` leg, same amount, days apart | One card payment split into two half-entries. Should be a single `bank -> card` transfer. Merging them is a manual edit. |
| `dup-id` | two transactions share an `import_id` or `fitid` | A near-certain double import. Show both dates; deleting one is a manual edit after the user confirms. |
| `dup-fuzzy` | same distinctive payee, amount and account, twice on one day | A weaker signal - could be two real charges. Present for review, capped; the `#` note says how many more. |
| `no-interest-split` | loan/mortgage principal payments with no interest posting | The loan needs an amortization schedule splitting each payment into interest and principal. Manual - see the loan's statements. |
| `no-loan-origination` | a loan account's first activity is a payment, not a disbursement | The opening principal was never booked. Manual. |
| `no-assertion` | an active asset/liability account has never had a balance assertion | Offer to add one (see Fixes) once the user gives a confirmed balance. |
| `recentassertions` | an account that has assertions has none in the last 7 days | Same fix path: a fresh confirmed balance. |
| `date-in-desc` | the description embeds a date well off the transaction date | The bank's posting date. Recommend recording it as a secondary date (`date2`); the ledger currently uses none. |
| `card-span` | a credit-card account's active range, plus any multi-month gap | No `card` tag exists, so a reissue or a second cardholder can only be confirmed with the user. Ask. |
| `undeclared-tags` / `undeclared-payees` | opt-in strict checks the ledger has not adopted | Informational. Mention once; do not push the user to adopt them. |

## Reconciling against statements

For every `wrong-sign` (unexplained), `late-lump-adjustment`, `no-assertion`
and `recentassertions` finding, the question is "what is this account's real
balance?". Answer it in this order:

1. **A balance assertion already in the journal.** `query` `report: "print"`
   for the account and look for a `= AMOUNT` posting. If one exists and
   `strict-check` passed, that balance is trusted.
2. **A statement file under `files/`.** Transactions carry a `related_file:`
   tag pointing at the statement they came from. For a `.pdf`, use the
   `extract_text` tool to pull the ending balance. For `.ofx` / `.qfx` /
   `.csv`, read the file with `bash` (`grep -i 'ledger.*bal\|<LEDGERBAL>'` for
   OFX/QFX) - `extract_text` does not handle those.
3. **Neither.** Say so. Ask the user for the current balance from their bank,
   or leave the finding open in `ledger-audit.md` as "needs a statement".

Never guess a balance from surrounding transactions - that is exactly how the
drift being audited crept in.

## Writing ledger-audit.md

Write the file at the workspace root (same place as `memory.md` and
`budget.md`), as plain readable markdown:

```markdown
# Ledger audit

Run: 2026-09-01
hledger: 1.52.1
Findings: 1 high, 30 warn, 36 info

## Fix now (high)

- **`liabilities:credit-card:united` balance is +220.90, should be negative.**
  No cause found by the other checks. Reconcile against the latest United
  statement (no `related_file` on recent entries - ask the user).

## Worth fixing (warn)

### Historical balances left wrong by lump corrections
- `liabilities:credit-card:citi` - wrong sign at every year end 2019-2025.
  The 2026-08 "balance correction per OFX" entry fixed today only. To make
  historical queries correct, the correction must be distributed across the
  periods it belongs to.
- ... (one bullet per account)

### Missing loan amortization
- `liabilities:mortgage` - 38 principal payments (2024-01..2027-02) with no
  interest split, and no origination entry. Needs an amortization schedule
  built from the loan statements.

### Categorisation backlog (hand to payee-audit)
- 14 accounts have postings offset to `equity:opening-balances` with no real
  counterparty - `assets:bank:bayfed` (811, 2019-01..2023-12), ... Run
  `payee-audit` to assign real accounts.

## Review (info)

- **Possible duplicates** - 20 same-day repeats listed, 79 more not shown.
  Spot-check these against the bank; a real double-import should be deleted.
- **No balance assertion** - 19 active accounts. Add one when the user
  confirms a balance.
- **Card spans** - `liabilities:credit-card:capital-one` active 2019-04..
  2026-08 with a gap at 2022-03..2022-09. Confirm whether the card was
  reissued.

## Not defects

- The journal records no secondary dates, so bank posting date is never
  distinguished from transaction date.
- `bs` net worth at old year-ends is unreliable because fund commodities lack
  historical prices - a valuation gap, not a sign problem.
```

Keep it to what the audit found. One section per severity, grouped by theme
inside `warn`. Every bullet names the account and says what the fix is, even
when the fix is manual.

## Offering fixes

After the user has read the report, offer to apply the fixes that are
mechanically safe. Only these:

1. **Recategorise `equity-plug` postings** - this is `payee-audit`'s job, not
   this skill's. Offer to run `payee-audit` next; it uses
   `bulk_edit_transactions` `change_account` and validates each batch.
2. **Add a balance assertion** - only after the user gives a confirmed balance
   and `query` shows the ledger already matches it. Use the
   `add_balance_assertions` tool. hledger rejects an assertion that does not
   hold, so a wrong number fails loudly rather than corrupting anything.
3. **Set transaction status** - `bulk_edit_transactions` `set_status` for
   pending/cleared hygiene, if the user asks.

For `missing-type`: give the user the exact lines to add to
`accounts.journal` (`account <name>` already exists; add `    type:X` under
it), and stop. This skill does not edit journal files directly - there is no
tool that does it with validate-and-revert safety.

Everything else - distributing a lump correction, building an amortization
schedule, merging a broken payment chain, deleting a duplicate, booking a loan
origination, renaming an account - is a manual edit. Describe it precisely;
do not attempt it.

## Anti-patterns

- **Never reconcile a balance by posting a lump sum to
  `equity:opening-balances` dated today.** It fixes the current balance and
  leaves every historical query wrong - it is the exact defect the
  `late-lump-adjustment` and `historical-sign` checks exist to catch. If a
  balance is wrong, the correction belongs in the period(s) where the error
  actually happened.
- **Never invent a target expense/income account** for an `equity-plug`
  posting. Hand it to `payee-audit`.
- **Never guess a real-world balance** from the transactions around it.

## Boundaries

- **Read-only while detecting and reporting.** The only file this skill writes
  in the workspace is `ledger-audit.md`. Never edit, validate, or commit the
  journal while running the audit or writing the report.
- **Confirm before any fix.** After presenting findings, apply only the fixes
  in "Offering fixes", and only the ones the user picks.
- The audit runs `hledger` directly; it writes nothing. If `hledger` is not
  available, say so and stop.
- If the journal has no transactions, say there is nothing to audit yet.
