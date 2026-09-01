#!/usr/bin/env python3
"""Structural audit of an hledger journal.

Usage:
    python3 audit_ledger.py --journal <path/to/main.journal> [--today YYYY-MM-DD]

Prints a findings report to stdout: a `#` summary line, then one tab-separated
row per finding

    severity <TAB> check <TAB> scope <TAB> count <TAB> detail

sorted most-actionable first, then `#` footnotes. Exit code is 0 even when
findings exist (a finding is data, not a script error); non-zero only on an
operational failure (hledger missing, journal unreadable).

Why this script runs hledger itself, unlike the other skills' scripts (which
parse a dump the `query` tool already produced): a structural audit has to see
the whole history at once - 2019 balances, year-by-year sign drift, loan
payments from years ago. Piping a full-history `hledger print` back through the
agent would be tens of thousands of lines every run. Instead this script shells
out to hledger (confirmed on PATH by the `query`/`validate` tools, which do the
same) and returns only the findings. It needs Python's standard library only.

The report is input to the model, not the final word: which findings matter,
how to group them for the user, and what to fix are decided in SKILL.md. In
particular this script never proposes a target expense/income account for a
mis-offset posting - that judgment belongs to the `payee-audit` skill.
"""
import argparse
import csv
import io
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

# ---- tunables ---------------------------------------------------------------

# Balances at or below this magnitude are treated as noise for the sign checks
# (rounding, a small lingering card credit). Currency-agnostic.
SIGN_NOISE = 1.0
# A single equity:opening-balances posting at least this large whose description
# reads like a reconciliation plug ("balance correction per OFX", "adjustment")
# is flagged as a lump adjustment - the anti-pattern that fixes today's balance
# while leaving every historical query wrong.
LUMP_MIN = 500.0
# Payment-chain halves must land within this many days of each other.
CHAIN_WINDOW_DAYS = 5
# Same-day duplicate candidate: identical normalised payee, |amount|, account,
# on the same date. Below this amount the same charge twice in a day is common
# and uninteresting; above it a repeat is worth a look.
DUP_MIN_AMOUNT = 25.0
DUP_REPORT_CAP = 20
# A plug within this many days of an account's first activity is its opening
# balance, not a late correction.
LUMP_LAG_MIN_DAYS = 30
# An account whose most recent posting is within this many days of `today`
# counts as active (so a missing balance assertion is worth flagging).
ACTIVE_DAYS = 120
# A description date token this many days off the transaction date is flagged.
DESC_DATE_SLACK_DAYS = 4

CLASS_OF_TYPE = {"A": "asset", "L": "liability", "E": "equity", "R": "income", "X": "expense"}
CLASS_OF_PREFIX = {
    "assets": "asset",
    "liabilities": "liability",
    "equity": "equity",
    "income": "income",
    "revenues": "income",
    "revenue": "income",
    "expenses": "expense",
    "expense": "expense",
}
# Expected sign of a *closing balance* per account class, standard convention.
EXPECTED_SIGN = {"asset": 1, "expense": 1, "liability": -1, "income": -1, "equity": -1}

OPENING_DESC_RE = re.compile(
    r"open(ing)?\s*balance|balance correction|balance assertion|contra|reconcil", re.I
)
# The reconciliation-plug anti-pattern: a correction booked as one dated lump.
LUMP_DESC_RE = re.compile(
    r"correction|adjustment|reconcil|true[\s-]?up|per\s+(qfx|ofx|statement|stmt)", re.I
)
# Generic bank descriptions that repeat legitimately - not duplicate signal.
GENERIC_PAYEE_RE = re.compile(
    r"^(internal |external )?(transfer|atm|withdrawal|deposit|payment|check|"
    r"e[- ]?check|ach|autopay|bill pay|online payment|wire|zelle|venmo|paypal|"
    r"\w+ state tax|irs|refund)\b",
    re.I,
)
# "interest" as a whole word anywhere in the account path: expenses:interest,
# expenses:financial:mortgage-interest, interest-expense, ...
INTEREST_ACCT_RE = re.compile(r"(?:^|[:_-])interest(?:$|[:_-])", re.I)
LOAN_ACCT_RE = re.compile(r"^liabilities:(mortgage|loan)(:|$)", re.I)
CARD_ACCT_RE = re.compile(r"^liabilities:credit-card:", re.I)
EQUITY_PLUG = "equity:opening-balances"


# ---- finding accumulator --------------------------------------------------


class Findings:
    ORDER = {"high": 0, "warn": 1, "info": 2}

    def __init__(self):
        self._rows = []
        self._notes = []

    def add(self, severity, check, scope, count, detail):
        assert severity in self.ORDER, severity
        self._rows.append((severity, check, scope, int(count), detail))

    def note(self, text):
        self._notes.append(text)

    def render(self):
        rows = sorted(self._rows, key=lambda r: (self.ORDER[r[0]], r[1], r[2]))
        out = io.StringIO()
        by_sev = Counter(r[0] for r in rows)
        summary = ", ".join(f"{by_sev[s]} {s}" for s in ("high", "warn", "info") if by_sev[s])
        print(f"# ledger-audit: {len(rows)} findings ({summary or 'none'})", file=out)
        print("# severity\tcheck\tscope\tcount\tdetail", file=out)
        for sev, check, scope, count, detail in rows:
            print(f"{sev}\t{check}\t{scope}\t{count}\t{detail}", file=out)
        for n in self._notes:
            print(f"# note: {n}", file=out)
        return out.getvalue()


# ---- hledger plumbing ---------------------------------------------------------


class HledgerError(RuntimeError):
    pass


def run_hledger(journal, args):
    """Return (exit_code, stdout, stderr). Raises HledgerError if hledger is absent."""
    try:
        p = subprocess.run(
            ["hledger", *args, "-f", journal],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise HledgerError("hledger not found on PATH") from e
    return p.returncode, p.stdout, p.stderr


def check_pass(journal, names):
    """Run `hledger check <names>`; return (ok, first_message).

    hledger's failure text is the error line plus a source excerpt; keep only
    the `hledger: Error: file:line:` locator, which is all the report needs.
    """
    code, _out, err = run_hledger(journal, ["check", *names])
    if code == 0:
        return True, ""
    first = next((ln.strip() for ln in err.splitlines() if ln.strip()), "check failed")
    first = re.sub(r"^hledger:\s*", "", first)
    return False, first[:160]


# ---- amount / balance parsing ---------------------------------------------------

_NUM_RE = re.compile(r"-?\d[\d.,]*\d|-?\d")


def parse_amount(text):
    """('-1,234.56 USD' | 'USD -1.234,56' | '$-10') -> (float, commodity) or None.

    Handles both '1,234.56' and '1.234,56' decimal styles. Ignores any cost or
    price after '@'. Commodity is whatever non-numeric token sits next to the
    number (symbol or code), or '' if there is none.
    """
    if text is None:
        return None
    head = text.split("@", 1)[0].strip()
    if not head:
        return None
    m = _NUM_RE.search(head)
    if not m:
        return None
    raw = m.group(0)
    if "," in raw and "." in raw:
        # The rightmost separator is the decimal point.
        dec = "," if raw.rfind(",") > raw.rfind(".") else "."
    elif "," in raw:
        # A lone comma: decimal if it looks like exactly two trailing digits.
        dec = "," if re.search(r",\d{2}$", raw) else ""
    else:
        dec = "."
    norm = raw
    if dec == ",":
        norm = raw.replace(".", "").replace(",", ".")
    elif dec == ".":
        norm = raw.replace(",", "")
    else:
        norm = raw.replace(",", "")
    try:
        value = float(norm)
    except ValueError:
        return None
    commodity = (head[: m.start()] + " " + head[m.end() :]).strip()
    commodity = commodity.strip("$ ").strip() or ("$" if "$" in head else "")
    return value, commodity


def parse_balance_cell(cell):
    """A balance-report CSV cell -> list of (float, commodity).

    hledger writes a multi-commodity balance as newline-separated amounts inside
    one quoted field; a zero balance is '0'.
    """
    out = []
    for line in (cell or "").splitlines():
        line = line.strip()
        if not line or line == "0":
            continue
        parsed = parse_amount(line)
        if parsed:
            out.append(parsed)
    return out


# ---- journal directive parsing ----------------------------------------------


def _iter_journal_files(main_path):
    """Yield main_path then every file it pulls in via `include` (one level, then
    their includes too - hledger allows nesting)."""
    seen = set()
    stack = [main_path]
    while stack:
        path = stack.pop()
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(real):
            continue
        seen.add(real)
        yield real
        base = os.path.dirname(real)
        try:
            with open(real, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for m in re.finditer(r"^\s*include\s+(.+?)\s*$", text, re.M):
            inc = m.group(1).strip().strip('"')
            stack.append(inc if os.path.isabs(inc) else os.path.join(base, inc))


def parse_account_directives(main_path):
    """-> dict account -> {'type': 'A'|... or None}. Covers only *declared*
    accounts; an account used but never declared won't appear here."""
    accounts = {}
    for jf in _iter_journal_files(main_path):
        with open(jf, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        current = None
        for line in lines:
            mem = re.match(r"^\s*account\s+(.+?)\s*(;.*)?$", line)
            if mem:
                current = mem.group(1).strip()
                accounts.setdefault(current, {"type": None})
                # A type: on the same line, in the trailing comment.
                t = re.search(r"\btype:\s*([A-Za-z])", mem.group(2) or "")
                if t:
                    accounts[current]["type"] = t.group(1).upper()
                continue
            if current is not None:
                t = re.match(r"^\s+;?\s*type:\s*([A-Za-z])", line)
                if t:
                    accounts[current]["type"] = t.group(1).upper()
                    continue
                if line.strip() and not line.startswith((" ", "\t", ";")):
                    current = None
    return accounts


def classify(account, directives):
    """Account class ('asset'|'liability'|'equity'|'income'|'expense'|'') from
    its type: directive, falling back to its name prefix."""
    d = directives.get(account)
    if d and d.get("type") in CLASS_OF_TYPE:
        return CLASS_OF_TYPE[d["type"]]
    prefix = account.split(":", 1)[0].lower()
    return CLASS_OF_PREFIX.get(prefix, "")


# ---- `hledger print` parsing ------------------------------------------------

_TXN_HEAD_RE = re.compile(
    r"^(?P<date>\d{4}[-/.]\d{2}[-/.]\d{2})"
    r"(?:[=](?P<date2>\d{4}[-/.]\d{2}[-/.]\d{2}))?"
    r"\s*(?P<status>[*!])?\s*"
    r"(?:\((?P<code>[^)]*)\)\s*)?"
    r"(?P<desc>.*)$"
)


def _tags_from_comment(text, into):
    for m in re.finditer(r"([A-Za-z0-9_-]+):\s*([^,]*)", text):
        into.setdefault(m.group(1), m.group(2).strip())


def parse_print(text):
    """`hledger print` output -> list of transaction dicts:

        {date, date2, status, code, description, payee, tags{},
         postings: [{account, amount, commodity, has_assertion, tags{}}]}
    """
    txns = []
    cur = None
    for line in text.splitlines():
        if not line.strip():
            if cur:
                txns.append(cur)
                cur = None
            continue
        head = _TXN_HEAD_RE.match(line)
        if head and cur is None:
            desc = head.group("desc").strip()
            comment = ""
            if ";" in desc:
                desc, comment = desc.split(";", 1)
                desc = desc.strip()
            cur = {
                "date": head.group("date").replace("/", "-").replace(".", "-"),
                "date2": (head.group("date2") or "").replace("/", "-").replace(".", "-") or None,
                "status": head.group("status") or "",
                "code": head.group("code") or "",
                "description": desc,
                "payee": desc.split("|", 1)[0].strip(),
                "tags": {},
                "postings": [],
            }
            if comment:
                _tags_from_comment(comment, cur["tags"])
            continue
        if cur is None:
            continue
        stripped = line.strip()
        if stripped.startswith(";"):
            _tags_from_comment(stripped[1:], cur["tags"])
            continue
        # A posting line: account, then >=2 spaces, then amount (maybe absent).
        pcomment = ""
        if ";" in stripped:
            stripped, pcomment = stripped.split(";", 1)
            stripped = stripped.rstrip()
        m = re.match(r"^(.+?)(?:\s{2,}(.*))?$", stripped)
        if not m:
            continue
        account = m.group(1).strip()
        rest = (m.group(2) or "").strip()
        has_assertion = "=" in rest
        amount_text = rest.split("=", 1)[0].strip() if has_assertion else rest
        parsed = parse_amount(amount_text) if amount_text else None
        ptags = {}
        if pcomment:
            _tags_from_comment(pcomment, ptags)
        cur["postings"].append(
            {
                "account": account,
                "amount": parsed[0] if parsed else None,
                "commodity": parsed[1] if parsed else "",
                "has_assertion": has_assertion,
                "tags": ptags,
            }
        )
    if cur:
        txns.append(cur)
    for t in txns:
        _infer_missing_amount(t)
    return txns


def _infer_missing_amount(t):
    """`hledger print` leaves the balancing posting's amount blank. If exactly
    one posting is blank and the rest are one commodity, fill it by balancing -
    the sign checks and the lump check need every posting to carry a number."""
    blank = [p for p in t["postings"] if p["amount"] is None]
    known = [p for p in t["postings"] if p["amount"] is not None]
    if len(blank) != 1 or not known:
        return
    commodities = {p["commodity"] for p in known}
    if len(commodities) != 1:
        return
    blank[0]["amount"] = -round(sum(p["amount"] for p in known), 4)
    blank[0]["commodity"] = next(iter(commodities))


def _d(iso):
    return datetime.strptime(iso, "%Y-%m-%d").date()


def norm_payee(p):
    return re.sub(r"[^a-z0-9]+", "", (p or "").lower())


# ---- individual checks -----------------------------------------------------


def check_hledger_builtins(journal, f):
    ok, msg = check_pass(journal, ["--strict"])
    if not ok:
        f.add("high", "strict-check", "journal", 1, f"hledger check --strict fails: {msg}")
    for name, sev, label in [
        ("ordereddates", "warn", "transactions out of date order within a file"),
        ("uniqueleafnames", "info", "two accounts share a leaf name"),
        ("recentassertions", "warn", "an account with assertions has none in the last 7 days"),
    ]:
        ok, msg = check_pass(journal, [name])
        if not ok:
            f.add(sev, name, "journal", 1, f"{label}: {msg}")
    for name in ("tags", "payees"):
        ok, msg = check_pass(journal, [name])
        if not ok:
            f.add("info", f"undeclared-{name}", "journal", 1, f"opt-in strictness not adopted: {msg}")


def check_missing_type(directives, used_accounts, f):
    missing = sorted(a for a in directives if directives[a]["type"] is None)
    if missing:
        f.add(
            "warn",
            "missing-type",
            "accounts.journal",
            len(missing),
            "declared without a type: directive - " + ", ".join(missing[:12]) + ("..." if len(missing) > 12 else ""),
        )
    undeclared = sorted(a for a in used_accounts if a not in directives)
    if undeclared:
        f.add(
            "warn",
            "undeclared-account",
            "journal",
            len(undeclared),
            "used in postings but never declared - " + ", ".join(undeclared[:12]) + ("..." if len(undeclared) > 12 else ""),
        )


def _balance_csv(journal, extra_args):
    """Run a `bal ... -O csv --layout bare` report; return (period_labels, rows)
    where each row is (account, commodity, [cell, ...]). -I so a broken
    assertion does not blank the report."""
    code, out, err = run_hledger(journal, [*extra_args, "-I"])
    if code != 0:
        raise HledgerError(err.strip().splitlines()[0] if err.strip() else "balance query failed")
    reader = list(csv.reader(io.StringIO(out)))
    if not reader:
        return [], []
    periods = reader[0][2:]
    rows = [(r[0], r[1], r[2:]) for r in reader[1:] if len(r) >= 3]
    return periods, rows


def _wrong_sign(value, want):
    return abs(value) > SIGN_NOISE and (value > 0) != (want > 0)


def check_closing_signs(journal, directives, today, explained, f):
    """Every account's closing balance must carry the sign its class implies.
    A wrong sign is a symptom - the detail names the cause another check found,
    or says it is unexplained."""
    _p, rows = _balance_csv(
        journal,
        ["bal", "-H", "-e", (today + timedelta(days=1)).isoformat(),
         "-O", "csv", "--no-total", "--layout", "bare"],
    )
    for account, commodity, cells in rows:
        cls = classify(account, directives)
        want = EXPECTED_SIGN.get(cls)
        if want is None or account == EQUITY_PLUG or not cells:
            continue
        parsed = parse_amount(f"{cells[0]} {commodity}")
        if not parsed or not _wrong_sign(parsed[0], want):
            continue
        value = parsed[0]
        cause = explained.get(account, "unexplained - reconcile against a real statement")
        # A small credit balance on a card is a real (if unusual) state, not a bug.
        sev = "info" if (cls == "liability" and CARD_ACCT_RE.match(account) and abs(value) < 200) else "high"
        f.add(sev, "wrong-sign", account, 1,
              f"closing balance {value:+.2f} {commodity}, expected "
              f"{'positive' if want > 0 else 'negative'}; cause: {cause}")


def check_historical_signs(journal, directives, today, f):
    """An asset or liability whose cumulative balance had the wrong sign at some
    past year end - the fingerprint of a correction booked as one late lump
    entry instead of distributed across the periods it belongs to."""
    periods, rows = _balance_csv(
        journal,
        ["bal", "-Y", "-H", "-e", today.isoformat(),
         "-O", "csv", "--no-total", "--layout", "bare"],
    )
    for account, commodity, cells in rows:
        cls = classify(account, directives)
        want = EXPECTED_SIGN.get(cls)
        if want is None or cls not in ("asset", "liability") or account == EQUITY_PLUG:
            continue
        bad = []
        for label, cell in zip(periods, cells):
            parsed = parse_amount(f"{cell} {commodity}")
            if parsed and _wrong_sign(parsed[0], want):
                bad.append(label)
        if bad:
            f.add("warn", "historical-sign", account, len(bad),
                  f"wrong-sign {commodity} balance at year end {', '.join(bad)} - "
                  "a late lump correction fixes today's balance but not these")


def opening_dates(txns):
    """First posting date seen for each account."""
    first = {}
    for t in txns:
        for p in t["postings"]:
            first.setdefault(p["account"], t["date"])
            if t["date"] < first[p["account"]]:
                first[p["account"]] = t["date"]
    return first


def check_equity_plugs(txns, directives, first_seen, explained, f):
    """A two-posting transaction whose only offset is equity:opening-balances and
    that is neither the account's genuine opening entry nor a reconciliation
    lump (check_lump_adjustments owns those). The real income/expense/transfer
    counterparty is missing; the category choice is payee-audit's job, so this
    check only counts and locates them, per account."""
    per_account = Counter()
    date_span = {}
    for t in txns:
        if len(t["postings"]) != 2:
            continue
        accts = [p["account"] for p in t["postings"]]
        if EQUITY_PLUG not in accts or accts[0] == accts[1]:
            continue
        other = accts[0] if accts[1] == EQUITY_PLUG else accts[1]
        if other == EQUITY_PLUG:
            continue
        if OPENING_DESC_RE.search(t["description"]) or LUMP_DESC_RE.search(t["description"]):
            continue
        cls = classify(other, directives)
        # A balance/liability account's first-ever entry offset to the plug is a
        # real opening balance. An expense/income account has no such thing - a
        # plug there is always a missing category, whatever its date.
        if cls in ("asset", "liability", "equity") and t["date"] == first_seen.get(other):
            continue
        per_account[other] += 1
        lo, hi = date_span.get(other, (t["date"], t["date"]))
        date_span[other] = (min(lo, t["date"]), max(hi, t["date"]))
        explained.setdefault(other, "offset to equity:opening-balances instead of the real counterparty")
    for acct, n in per_account.most_common():
        lo, hi = date_span[acct]
        leg = classify(acct, directives) or "account"
        f.add("warn", "equity-plug", acct, n,
              f"{n} {leg} postings offset straight to equity:opening-balances "
              f"({lo}..{hi}); the real counterparty is missing - hand to payee-audit "
              "for categorisation")


def check_broken_chains(txns, f):
    """A bank->equity leg and an equity->card leg, same amount, a few days apart,
    neither marked pending: - one card payment split into two half-entries."""
    outs, ins = [], []  # (date, amount, account, payee)
    for t in txns:
        if "pending" in t["tags"]:
            continue
        if len(t["postings"]) != 2:
            continue
        by_acct = {p["account"]: p for p in t["postings"]}
        if EQUITY_PLUG not in by_acct:
            continue
        real = next((p for a, p in by_acct.items() if a != EQUITY_PLUG), None)
        if real is None or real["amount"] is None:
            continue
        rec = (t["date"], round(abs(real["amount"]), 2), real["account"], t["payee"])
        if real["account"].startswith("assets:") and real["amount"] < 0:
            outs.append(rec)
        elif CARD_ACCT_RE.match(real["account"]) and real["amount"] > 0:
            ins.append(rec)
    pairs = []
    used = set()
    for i, (d1, amt1, a1, _p1) in enumerate(outs):
        for j, (d2, amt2, a2, _p2) in enumerate(ins):
            if j in used or amt1 != amt2:
                continue
            if abs((_d(d1) - _d(d2)).days) <= CHAIN_WINDOW_DAYS:
                pairs.append((d1, amt1, a1, a2))
                used.add(j)
                break
    for d1, amt, a1, a2 in pairs:
        f.add("warn", "broken-chain", a2, 1,
              f"{d1}: {a1} -> equity -> {a2} for {amt:.2f} should be one {a1} -> {a2} transfer")


def check_duplicates(txns, f):
    by_id = defaultdict(list)
    for t in txns:
        for key in ("import_id", "fitid"):
            v = t["tags"].get(key)
            if v:
                by_id[(key, v)].append(t["date"])
    for (key, v), dates in sorted(by_id.items()):
        if len(dates) > 1:
            f.add("high", "dup-id", key, len(dates),
                  f"{len(dates)} transactions share {key}:{v} ({', '.join(sorted(dates))})")

    # Fuzzy tier: the same distinctive payee posting the same amount to the same
    # account twice on one calendar day, with no import id to tell them apart.
    # Same-day is the tight constraint - a recurring bill never repeats same-day,
    # so this isolates likely double-imports without drowning in normal activity.
    seen = defaultdict(set)  # (date, payee, amount, account) -> {import ids or ""}
    candidates = []
    for t in txns:
        payee = t["payee"]
        if not payee or GENERIC_PAYEE_RE.match(payee):
            continue
        ident = t["tags"].get("import_id") or t["tags"].get("fitid") or ""
        for p in t["postings"]:
            if p["account"] == EQUITY_PLUG:
                continue
            if p["amount"] is None or abs(p["amount"]) < DUP_MIN_AMOUNT:
                continue
            k = (t["date"], norm_payee(payee), round(abs(p["amount"]), 2), p["account"])
            prior = seen[k]
            if prior and (ident == "" or "" in prior or ident in prior):
                candidates.append((t["date"], payee, abs(p["amount"]), p["account"]))
            prior.add(ident)
    # collapse repeats of the same (date, payee, amount, account) to one line
    uniq = list(dict.fromkeys(candidates))
    for d, payee, amt, acct in uniq[:DUP_REPORT_CAP]:
        f.add("info", "dup-fuzzy", acct, 1,
              f"{d}: {payee} posts {amt:.2f} to {acct} more than once the same day")
    if len(uniq) > DUP_REPORT_CAP:
        f.note(f"dup-fuzzy: {len(uniq) - DUP_REPORT_CAP} more same-day repeats not listed")
    if not uniq:
        f.note("no same-day duplicate candidates found")


def check_loan_amortisation(txns, first_seen, f):
    loan_accts = sorted({p["account"] for t in txns for p in t["postings"] if LOAN_ACCT_RE.match(p["account"])})
    for loan in loan_accts:
        no_split = []
        real_origination = False
        plug_origination = False
        for t in txns:
            legs = {p["account"]: p for p in t["postings"]}
            if loan not in legs or legs[loan]["amount"] is None:
                continue
            amt = legs[loan]["amount"]
            if amt < 0:  # liability grew: a draw, the opening principal, or a plug
                if LUMP_DESC_RE.search(t["description"]) or EQUITY_PLUG in legs:
                    plug_origination = True
                else:
                    real_origination = True
                continue
            if amt > 0 and not any(INTEREST_ACCT_RE.search(a) for a in legs):
                no_split.append(t["date"])
        if no_split:
            f.add("warn", "no-interest-split", loan, len(no_split),
                  f"principal payments with no interest posting ({min(no_split)}..{max(no_split)}); "
                  "add an amortization schedule splitting each payment")
        if not real_origination:
            hint = (" (a correction/plug entry looks like the draw, so this may be "
                    "masked)" if plug_origination else "")
            f.add("warn", "no-loan-origination", loan, 1,
                  f"first activity {first_seen.get(loan, '?')} is a payment, not a disbursement - "
                  f"opening principal was never booked{hint}")


def check_lump_adjustments(txns, first_seen, explained, f):
    """A reconciliation plug: a sizeable posting to equity:opening-balances whose
    description says it is a correction / adjustment / "per OFX". It squares the
    account with a statement as of its date and leaves every earlier period
    wrong - see the historical-sign check."""
    for t in txns:
        if not LUMP_DESC_RE.search(t["description"]):
            continue
        plug = [p for p in t["postings"] if p["account"] == EQUITY_PLUG and p["amount"] is not None]
        others = [p["account"] for p in t["postings"] if p["account"] != EQUITY_PLUG]
        if len(plug) != 1 or abs(plug[0]["amount"]) < LUMP_MIN or not others:
            continue
        target = others[0]
        fs = first_seen.get(target)
        if fs and 0 <= (_d(t["date"]) - _d(fs)).days < LUMP_LAG_MIN_DAYS:
            continue  # this is the account's opening balance, correctly placed
        lag = f", {(_d(t['date']) - _d(fs)).days} days after it opened" if fs else ""
        explained.setdefault(target, f"reconciled by a lump correction on {t['date']}, not distributed")
        f.add("warn", "late-lump-adjustment", target, 1,
              f"{t['date']}: {abs(plug[0]['amount']):.2f} booked to equity:opening-balances{lag} - "
              "distribute the correction across the periods it belongs to instead")


def check_assertions_coverage(txns, directives, today, f):
    last_seen, has_assertion = {}, set()
    for t in txns:
        for p in t["postings"]:
            a = p["account"]
            if a not in last_seen or t["date"] > last_seen[a]:
                last_seen[a] = t["date"]
            if p["has_assertion"]:
                has_assertion.add(a)
    gap = []
    for a, last in last_seen.items():
        cls = classify(a, directives)
        if cls not in ("asset", "liability"):
            continue
        if (today - _d(last)).days > ACTIVE_DAYS or a in has_assertion:
            continue
        gap.append(a)
    if gap:
        f.add("info", "no-assertion", "active accounts", len(gap),
              "active asset/liability accounts with no balance assertion ever - "
              + ", ".join(sorted(gap)[:12]) + ("..." if len(gap) > 12 else ""))


_DESC_DATE_RES = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"), "mdy"),
]


def _desc_dates(text, txn_year):
    found = []
    for rx, kind in _DESC_DATE_RES:
        for m in rx.finditer(text):
            try:
                if kind == "ymd":
                    y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    mo, da, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    y += 2000 if y < 100 else 0
                if abs(y - txn_year) > 1:
                    continue
                found.append(date(y, mo, da))
            except ValueError:
                continue
    return found


def check_desc_dates(txns, f):
    any_date2 = any(t["date2"] for t in txns)
    hits = 0
    for t in txns:
        td = _d(t["date"])
        for dd in _desc_dates(t["description"], td.year):
            if abs((dd - td).days) > DESC_DATE_SLACK_DAYS:
                hits += 1
                if hits <= 8:
                    f.add("info", "date-in-desc", t["payee"] or "?", 1,
                          f"transaction dated {t['date']} but description says {dd.isoformat()} "
                          "- use a secondary date (date2) for the bank's posting date")
                break
    if not any_date2:
        f.note("journal uses no secondary dates (date2) at all - posting date vs "
               "transaction date is never distinguished")


def check_multi_card(txns, directives, f):
    """This ledger tags no posting with a card number or statement id, so a card
    reissue or a second cardholder collapsed into one account cannot be detected
    mechanically. Surface each credit-card account's active span and a
    multi-month dormancy (a plausible reissue seam) for the user to judge."""
    spans = defaultdict(list)
    for t in txns:
        for p in t["postings"]:
            if CARD_ACCT_RE.match(p["account"]):
                spans[p["account"]].append(t["date"])
    for account in sorted(spans):
        dates = sorted(spans[account])
        months = sorted({d[:7] for d in dates})
        gaps = []
        for a, b in zip(months, months[1:]):
            ay, am = int(a[:4]), int(a[5:7])
            by, bm = int(b[:4]), int(b[5:7])
            if (by - ay) * 12 + (bm - am) >= 3:
                gaps.append(f"{a}->{b}")
        detail = f"active {dates[0]}..{dates[-1]}, {len(dates)} postings"
        if gaps:
            detail += f"; multi-month gaps at {', '.join(gaps)} (possible card reissue)"
        f.add("info", "card-span", account, len(dates), detail)
    if spans:
        f.note("card identity is not tagged - reissued cards and extra cardholders "
               "on one account can only be confirmed with the user")


# ---- orchestration -------------------------------------------------------------


def audit(journal, today):
    f = Findings()

    code, ver, _ = run_hledger(journal, ["--version"])
    if code != 0:
        raise HledgerError("could not run `hledger --version`")

    # -I: a failing balance assertion otherwise aborts `print` and blinds every
    # parse-based check. Assertions are still audited via check_hledger_builtins.
    code, print_out, err = run_hledger(journal, ["print", "-I"])
    if code != 0:
        raise HledgerError(f"`hledger print` failed: {err.strip()}")
    txns = parse_print(print_out)
    if not txns:
        f.note("journal has no transactions")
        return f.render()

    directives = parse_account_directives(journal)
    used_accounts = {p["account"] for t in txns for p in t["postings"]}
    first_seen = opening_dates(txns)
    explained = {}  # account -> why its balance is off, filled in by earlier checks

    check_hledger_builtins(journal, f)
    check_missing_type(directives, used_accounts, f)
    # These populate `explained`, so run them before the sign checks that read it.
    check_lump_adjustments(txns, first_seen, explained, f)
    check_equity_plugs(txns, directives, first_seen, explained, f)
    check_broken_chains(txns, f)
    check_duplicates(txns, f)
    check_loan_amortisation(txns, first_seen, f)
    check_assertions_coverage(txns, directives, today, f)
    check_desc_dates(txns, f)
    check_multi_card(txns, directives, f)
    try:
        check_historical_signs(journal, directives, today, f)
        check_closing_signs(journal, directives, today, explained, f)
    except HledgerError as e:
        f.note(f"balance-report checks skipped: {e}")

    return f.render()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Structural audit of an hledger journal.")
    ap.add_argument("--journal", required=True, help="path to main.journal")
    ap.add_argument("--today", help="override today's date, YYYY-MM-DD")
    args = ap.parse_args(argv)

    today = _d(args.today) if args.today else date.today()
    if not os.path.isfile(args.journal):
        print(f"error: journal not found: {args.journal}", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(audit(args.journal, today))
    except HledgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
