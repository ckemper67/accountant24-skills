---
name: memory-review
description: Audits memory.md against the rules for what memory is for, then reports what has drifted and cleans up only what the user confirms. Finds duplicate and near-duplicate entries, transaction-level detail and payee-to-account mappings that belong in the ledger, one-off status notes and reconciliation worklogs that were never durable facts, relative dates, verbatim sentences that should be distilled, and structural problems like a topic split across two sections or a repeated heading. Ask things like "review my memory", "clean up memory", "is my memory tidy", or "what's cluttering my memory".
---

# Memory Review

Audit `memory.md` against the memory contract, show the user what has drifted
from it, and apply only the fixes they accept. The authority for what memory
is for is the `# Memory` section of your system prompt; this skill is the
procedure for checking memory against it and repairing it safely. Do not
restate those rules as your own - read them there and apply them.

This is a review, not a rewrite. You propose concrete edits; the user accepts
or rejects each one; you apply the accepted ones with targeted `edit`
operations.

## Reading memory

Read `memory.md` from the workspace with the `read` tool, even though its
content is already in the `<memory>` block. You need real line numbers so that
every fix is a targeted `edit` that touches only the affected lines.

If `memory.md` is missing or empty, say so and stop - there is nothing to
review.

## What belongs in memory

Memory holds four kinds of thing: user-stated facts, preferences,
categorization rules, and recurring arrangements. (This restates the
`# Memory` section of the system prompt for convenience; that section is
authoritative if the two ever diverge.)

Three kinds are named as never belonging there:

- transaction-specific context (belongs in a posting's description or tags),
- payee-to-account mappings (the ledger is the source of truth for those),
- anything else derivable from the workspace files (ledger, settings).

Everything else that is not one of the four positive kinds is out too - not
because a rule forbids it by name, but because memory is only for those four.
A reconciliation log, an "imported the March statement" note, or a "still
waiting on the user" reminder is none of the four, so it does not belong,
even though no line prohibits it word for word.

## What to look for

Go through memory entry by entry. For each problem below: recognise it, know
why it is a problem, know the fix.

### Duplicate and near-duplicate entries

- Looks like: the same fact as two bullets, often in different sections or
  with slightly different wording (a cabin listed under both Accounts and
  Properties; a rule stated twice).
- Why: the contract says to update or remove the existing entry on a topic,
  never add a near-duplicate.
- Fix: merge into the single most complete and correct entry, in the section
  where it best belongs; delete the others.

### Transaction-level detail

- Looks like: bullets about one payment, one invoice, one refund - amounts,
  dates, and counterparties of a specific transaction.
- Why: that context belongs in the posting's description or tags, not memory.
- Fix: delete the entry. If it encodes a durable rule ("Acme invoices
  quarterly"), keep only the distilled rule and drop the transaction.

### Payee-to-account mappings

- Looks like: "Payee X posts to Expenses:Y" lists.
- Why: the ledger already records where a payee's postings go; memory
  restating it will drift from the ledger.
- Fix: delete the mapping. A genuine categorization *rule* the user stated
  ("all coffee shops go to Expenses:Dining") stays - that is one of the four
  kinds. A lookup table mirroring ledger history goes.

### Workspace-derivable facts

- Looks like: account balances, totals, lists of accounts or commodities,
  anything a `query` or a look at settings would answer.
- Why: it is derivable from the workspace, so memory only adds a stale copy.
- Fix: delete it.

### One-off status notes and worklogs

- Looks like: "Imported the Q1 statements", "Reconciliation 2026-08: 12 of 18
  cleared", "Asked user about the Verizon charge, pending". Progress,
  checklists, and investigation notes.
- Why: none of these is a user-stated fact, preference, rule, or recurring
  arrangement. Memory is not a task tracker.
- Fix: delete the entry. If the exercise produced a durable rule, keep the
  rule and drop the status.

### Relative dates

- Looks like: "last week", "yesterday", "recently", "a few months ago", "this
  year".
- Why: the contract requires absolute dates only.
- Fix: replace with the absolute date (YYYY-MM-DD) if it can be determined
  from context; otherwise flag it for the user to supply.

### Verbatim sentences instead of distilled facts

- Looks like: a whole quoted request or a chatty multi-sentence bullet where
  one clause carries the fact.
- Why: the contract says to store the distilled fact, not the sentence.
- Fix: rewrite as one plain bullet stating the fact. Do not drop information -
  only phrasing.

### Structural problems

- Looks like: two headings for the same topic (a repeated `## Preferences`), a
  bullet filed under the wrong heading, a topic's entries scattered across
  several sections.
- Why: the contract wants `-` bullets grouped under one `##` section per
  topic.
- Fix: merge the duplicate sections, move stray bullets to the right section,
  keep one heading per topic. Preserve every real fact while doing so.

### Contradicted or outdated facts

- Looks like: two entries that cannot both be true; an entry the current
  ledger or a later entry contradicts.
- Why: a corrected or outdated fact must be fixed or deleted right away.
- Fix: keep the version consistent with the latest user input and the ledger;
  delete the other. If you cannot tell which is current, flag it - do not
  guess.

### Long is not the same as wrong

A long section is only a problem if its entries break a rule. A big list of
real categorization rules or a long roster of unified payee names the user
relies on is healthy memory - do not propose trimming it for size, and do not
count its length as a finding. Only flag entries in it that are duplicates,
transaction-level, derivable, or stale.

## Reporting

Present findings grouped by the categories above, in that order. For each
finding:

- quote the exact line(s) from `memory.md`,
- name the rule it breaks, in one phrase,
- show the proposed change: the replacement text, or "delete", or "merge into
  <the surviving entry>".

End with a count: N findings across M categories, and how many lines would be
removed versus rewritten. If memory is clean, say so plainly.

## Applying fixes

Only after the user has reviewed the report and said which findings to apply:

- one targeted `edit` per finding - change only the lines that finding names,
- merges: edit the surviving entry to be complete, then delete the others in
  their own edits,
- keep section headings and ordering otherwise untouched,
- apply the findings one at a time, and re-read `memory.md` before each edit
  in the sequence - every merge or deletion shifts the line numbers of the
  findings still to come, so line numbers from the report are stale after the
  first change.

Leave every finding the user did not accept exactly as it was.

## Boundaries

- Never modify `memory.md` with `write` or `bash`. `write` is only for the
  very first save of an empty memory; this skill always edits an existing
  file, line by line.
- Never delete a fact only because a section is long or an entry is wordy -
  wordiness is fixed by rewriting, length is not a defect at all.
- When you cannot tell whether something is a durable fact or transient
  clutter, keep it and flag it for the user. The user decides; the skill only
  surfaces.
- This skill touches `memory.md` only - never the journal, settings, or any
  other workspace file.
