# accountant24-skills

Custom skills for the [Accountant24](https://accountant24.com) agent.

Skills are reusable playbooks the agent follows step by step. Each skill is a
folder with a `SKILL.md` file: a `name` and `description` in the frontmatter,
then the instructions the agent follows. A skill folder may also carry scripts
that the agent runs as part of the skill.

Accountant24 follows the open [Agent Skills](https://agentskills.io) standard.

## Add these skills

1. Open **Settings -> Skills** in Accountant24.
2. Click **Add skill**.
3. Enter `ckemper67/accountant24-skills` (or the full GitHub URL).
4. Enable the skills you want.

Accountant24 downloads the repository and copies each skill folder it finds at
the repo root into `~/.accountant24/skills`.

Only add skills you trust: a skill can run commands with full access to your
workspace.

## Skills in this repo

<!-- Keep this list in sync with the top-level skill folders. -->

- **budget** - builds a monthly and yearly per-account spending budget from
  your last 13 months of expenses, writes it to `budget.md`, and refines it
  with your input. Bundles `compute_budget.py` (uses `python3` when present,
  with a no-Python fallback).
- **memory-review** - audits `memory.md` against the rules for what memory is
  for, reports what has drifted (duplicates, transaction-level detail, stale
  status notes, relative dates, structural problems), and applies only the
  fixes you confirm. Read-only until you approve each change.
- **payee-audit** - flags payees in the wrong or inconsistent expense account
  (or `expenses:uncategorized`), spots spelling variants of the same merchant
  and proposes unifying them first, then offers to apply the fixes with
  `bulk_edit_transactions`. Bundles `map_payees.py` (stdlib `python3`, with a
  by-hand fallback).
- **recurring-expenses** - detects the payments you make on a schedule (bills
  plus subscriptions) from 13 months of history and caches them in
  `recurring-expenses.md` at the workspace root. A builder for that cache, not
  a report; other skills read the file instead of re-deriving it. Bundles
  `detect_recurring.py` for the grouping and interval maths (uses `python3`
  when present, with a by-hand fallback).
- **weekly-recap** - a three-part weekly check-in: how net worth moved over
  the last 7 days, what you spent on and where, and which recurring payments
  are due in the next 7. Read-only; reads the `recurring-expenses` cache for
  the third part.

## Repository layout

```
accountant24-skills/
  README.md
  plugin.json         the Agent Plugins manifest (name, description)
  skills/
    <skill-name>/
      SKILL.md        required: name + description frontmatter, then the playbook
      *.py            optional: helper scripts the skill runs
```

- One skill per folder under `skills/`. The folder name must match the skill's
  `name:` (kebab-case).
- `name` must be usable as a folder name and must not collide with an
  Accountant24 built-in skill (the app rejects shadowing ones).
- `description` is what the agent routes on and what shows in the `/` menu:
  write it as the phrases a user would actually say.

## Authoring a skill

Model new skills on the app's built-in ones. The house pattern:

- A verb-first one-paragraph intro stating what the skill produces.
- Numbered procedure steps.
- A `## Reporting` section defining the output.
- A `## Boundaries` section with hard stops (for example: read-only, never
  modify the journal).

Prefer the agent's purpose-built tools (`query`, `add_transactions`, ...) over
raw file or shell access, and say so in the skill.
