#!/usr/bin/env python3
"""Turn an hledger `reg` expense dump into per-payee recurrence facts.

Usage: python3 detect_recurring.py <csv_file> [--today YYYY-MM-DD]

Input: the CSV hledger emits for
  query report:"reg" account_pattern:"Expenses" begin_date:<13 months ago>
        output_format:"csv"
i.e. columns txnidx, date, code, description, account, amount, total - one
row per posting. Payee is the description text before the first "|", matching
hledger's own payee/description split.

What this does (deterministic bookkeeping, so the model does not have to do
it by hand and get it subtly wrong across dozens of payees):

  * group positive postings by normalized payee + commodity (case and
    whitespace folded only - "NETFLIX" == "Netflix", but "NETFLIX.COM" stays
    its own row; deciding real-merchant merges is the model's job)
  * per group: posting count, distinct charge months, first/last date
  * intervals between consecutive charge dates -> a cadence guess
    (weekly / biweekly / monthly / quarterly / semiannual / yearly / irregular)
    and a regularity score (fraction of intervals that fit that cadence)
  * amount shape: identical / step (a price change, with the date) /
    banded (a metered bill, min-max) / irregular
  * approx monthly cost, normalized for the cadence
  * a status flag: active, or overdue (last charge older than the cadence
    implies - a candidate for "expected but not seen")
  * a recurrence guess: `yes` (>= 2 intervals, regularity >= 0.75, a clean
    identical/step amount), `weak` (a real cadence but thin evidence - only
    one interval, a wobbly regularity, or a banded amount), or `no`.

A group is a candidate when it was charged in 3+ distinct months, OR its gaps
already fit a cadence (so a 2-month-old ledger still yields its monthly bills,
as `weak`).

What this does NOT do - left to the model: merging spelling variants that
differ by more than case, deciding whether a `weak` row is real, classifying
bills vs subscriptions, naming a merged payee.

Amounts: either ',' or '.' may be the decimal mark (hledger follows the
journal's `decimal-mark`). "1,234.56" and "1.234,56" both parse to 1234.56.
A lone separator with exactly 3 trailing digits ("1,234" / "1.234") is read
as a thousands separator -> 1234; with 1-2 trailing digits it is the decimal
mark. A 3-decimal commodity written without a thousands separator (e.g.
"12.999 KWD") is therefore misread as 12999 - a known limitation.

Output: tab-separated, one line per candidate payee, sorted by recurrence
guess, then approx monthly cost DESCENDING, then payee and commodity (so the
order is stable across re-runs and unaffected by input row order). A leading
"#" header names the columns; a trailing "#" line reports groups dropped for
too little history and rows dropped while reading. Exits non-zero with a
message if no usable postings were read.

Standard library only - the vendored interpreter has no pip. If python3 is
not available, the skill falls back to doing this by hand.
"""
import csv
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from statistics import median

# One amount in a reg cell: an optional commodity ("$" or a 2-5 letter code,
# before or after), then a number that may carry ',' and '.' group/decimal
# marks. Cost annotations ("100 USD @ 0.92 EUR") are cut before matching.
_NUM = r"-?\d[\d.,]*\d|-?\d"
AMOUNT_RE = re.compile(rf"(?:(?P<pre>[A-Za-z]{{2,5}}|\$)\s*)?(?P<num>{_NUM})(?:\s*(?P<post>[A-Za-z]{{2,5}}))?")

# label: (min_days, max_days, days_per_period) - first bucket a median
# interval falls in wins; nothing matching is "irregular".
CADENCES = [
    ("weekly", 5, 10, 7.0),
    ("biweekly", 11, 18, 14.0),
    ("monthly", 24, 38, 30.44),
    ("quarterly", 78, 100, 91.31),
    ("semiannual", 160, 205, 182.62),
    ("yearly", 320, 400, 365.25),
]
DAYS_PER_MONTH = 30.44


def parse_number(text):
    """Parse a number string that may use ',' or '.' as the decimal mark and
    the other as a thousands separator. Returns a float, or None if `text`
    holds no digit.
    """
    text = text.strip()
    sign = -1.0 if text.startswith("-") else 1.0
    body = text.lstrip("+-").strip()
    if not any(c.isdigit() for c in body):
        return None
    has_comma, has_dot = "," in body, "." in body
    if has_comma and has_dot:
        # the separator that appears last is the decimal mark
        dec = "," if body.rfind(",") > body.rfind(".") else "."
        thou = "." if dec == "," else ","
        body = body.replace(thou, "").replace(dec, ".")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        head, _, tail = body.rpartition(sep)
        if body.count(sep) > 1 or (len(tail) == 3 and head.isdigit()):
            body = body.replace(sep, "")          # thousands grouping
        else:
            body = body.replace(sep, ".")          # decimal mark
    return sign * float(body)


def parse_amounts(cell):
    """Yield (commodity, value) for each amount in a reg amount cell.

    Commodity defaults to "?" when hledger emitted a bare number. A cost
    annotation ("... @ PRICE" / "... @@ TOTAL") is dropped before parsing so
    the exchange rate is not mistaken for a second charge.
    """
    cell = cell.strip()
    if not cell:
        return
    cell = cell.split("@", 1)[0]
    for m in AMOUNT_RE.finditer(cell):
        value = parse_number(m.group("num"))
        if value is None:
            continue
        commodity = m.group("pre") or m.group("post") or "?"
        yield commodity, value


def payee_of(description):
    return description.split("|", 1)[0].strip()


def norm_payee(payee):
    return " ".join(payee.split()).casefold()


def parse_date(s):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unparseable date: {s!r}")


def read_postings(path):
    """Return (postings, drops).

    postings: list of (payee_raw, norm, commodity, date, amount, account) for
    every positive expense posting in the reg CSV.
    drops: {reason: count} for rows skipped, so the caller can tell an empty
    result ("no recurring expenses") from an unreadable or wrong file.
    """
    out = []
    drops = {"no_payee_or_account": 0, "bad_date": 0, "no_positive_amount": 0}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"date", "description", "account", "amount"}
        if not required.issubset(reader.fieldnames or []):
            missing = required.difference(reader.fieldnames or [])
            raise SystemExit(f"error: CSV is missing column(s): {', '.join(sorted(missing))}")
        for row in reader:
            payee = payee_of(row.get("description", ""))
            account = (row.get("account") or "").strip()
            if not payee or not account:
                drops["no_payee_or_account"] += 1
                continue
            try:
                d = parse_date(row.get("date", ""))
            except ValueError:
                drops["bad_date"] += 1
                continue
            posted = False
            for commodity, value in parse_amounts(row.get("amount", "")):
                if value <= 0:  # keep charges only; refunds/credits are not the recurring series
                    continue
                out.append((payee, norm_payee(payee), commodity, d, value, account))
                posted = True
            if not posted:
                drops["no_positive_amount"] += 1
    return out, drops


def guess_cadence(intervals):
    """(label, days_per_period, regularity) from a list of day-gaps.

    regularity = fraction of gaps that fall in the matched cadence's day
    range; for an irregular series it is measured against a +/-50% band
    around the median gap instead.
    """
    if not intervals:
        return "single", 0.0, 0.0
    med = median(intervals)
    for label, lo, hi, dpp in CADENCES:
        if lo <= med <= hi:
            fit = sum(1 for iv in intervals if lo <= iv <= hi) / len(intervals)
            return label, dpp, round(fit, 2)
    lo, hi = 0.5 * med, 1.5 * med
    fit = sum(1 for iv in intervals if lo <= iv <= hi) / len(intervals)
    # med is >= 1: intervals are gaps between distinct sorted dates.
    return "irregular", med, round(fit, 2)


def classify_amounts(dated_amounts):
    """(shape, detail, representative) for a date-sorted list of (date, amount).

    shape is one of identical / step / banded / irregular.
    """
    vals = [round(a, 2) for _, a in dated_amounts]
    if len(set(vals)) == 1:
        return "identical", f"{vals[0]:.2f}", vals[0]

    transitions = []  # (new_value, date)
    level = vals[0]
    for (d, _), v in zip(dated_amounts, vals):
        if abs(v - level) > max(0.01, 0.005 * abs(level)):
            transitions.append((v, d))
            level = v

    levels = [vals[0]] + [v for v, _ in transitions]
    increasing = all(b > a for a, b in zip(levels, levels[1:]))
    decreasing = all(b < a for a, b in zip(levels, levels[1:]))
    if transitions and 1 <= len(transitions) <= 3 and (increasing or decreasing):
        detail = f"{vals[0]:.2f}" + "".join(f"->{v:.2f}@{d.isoformat()}" for v, d in transitions)
        return "step", detail, transitions[-1][0]

    lo, hi = min(vals), max(vals)
    rep = round(median(vals), 2)
    if lo > 0 and hi / lo <= 4.0:
        return "banded", f"{lo:.2f}-{hi:.2f}", rep
    return "irregular", f"{lo:.2f}-{hi:.2f}", rep


def analyze(postings, today):
    """postings: list of (payee_raw, commodity, date, amount, account) for one
    normalized payee+commodity group. Returns a dict of facts, or None if the
    group is not a recurrence candidate."""
    postings = sorted(postings, key=lambda p: p[2])
    raw_names = Counter(p[0] for p in postings)
    accounts = Counter(p[4] for p in postings)
    dates = [p[2] for p in postings]
    distinct_dates = sorted(set(dates))
    months = sorted({(d.year, d.month) for d in dates})
    intervals = [(b - a).days for a, b in zip(distinct_dates, distinct_dates[1:])]

    cadence, dpp, regularity = guess_cadence(intervals)

    # Normally a candidate needs 3+ charge months. A younger series still
    # counts if its gaps already point at a real cadence (a 2-month-old ledger
    # with one 30-day gap is a plausible monthly bill) - but with only one
    # interval it can never rate better than `weak` (see below).
    recognized = bool(intervals) and cadence not in ("irregular", "single")
    if len(months) < 3 and not recognized:
        return None

    shape, amount_detail, rep = classify_amounts([(p[2], p[3]) for p in postings])
    approx_monthly = rep * (DAYS_PER_MONTH / dpp) if dpp else rep

    last = distinct_dates[-1]
    gap = (today - last).days
    if cadence not in ("irregular", "single") and dpp:
        overdue_after = 1.5 * dpp
    elif intervals:
        overdue_after = 2.0 * median(intervals)
    else:
        overdue_after = None
    if overdue_after is not None and gap > overdue_after:
        status = f"overdue~{gap / dpp:.1f}x" if dpp else "overdue"
    else:
        status = "active"

    # A single interval (two charges) can land in a cadence bucket by
    # coincidence with regularity 1.0 - never enough for `yes`.
    enough_intervals = len(intervals) >= 2
    if cadence in ("irregular", "single") or regularity < 0.5:
        recurring = "no"
    elif enough_intervals and regularity >= 0.75 and shape in ("identical", "step"):
        recurring = "yes"
    else:
        recurring = "weak"

    name = raw_names.most_common(1)[0][0]
    acct_str = ", ".join(f"{a} ({n})" for a, n in accounts.most_common())
    return {
        "payee": name,
        "commodity": postings[0][1],
        "postings": len(postings),
        "months": len(months),
        "first": distinct_dates[0].isoformat(),
        "last": last.isoformat(),
        "cadence": cadence,
        "regularity": f"{regularity:.2f}",
        "shape": shape,
        "amount_detail": amount_detail,
        "approx_monthly": f"{approx_monthly:.2f}",
        "status": status,
        "recurring": recurring,
        "accounts": acct_str or "-",
    }


COLUMNS = [
    "recurring", "payee", "commodity", "postings", "months", "first", "last",
    "cadence", "regularity", "shape", "amount_detail", "approx_monthly",
    "status", "accounts",
]
_RANK = {"yes": 0, "weak": 1, "no": 2}


def build_report(postings, today):
    groups = defaultdict(list)
    for payee, norm, commodity, d, amount, account in postings:
        groups[(norm, commodity)].append((payee, commodity, d, amount, account))

    rows, skipped = [], 0
    for key in groups:
        fact = analyze(groups[key], today)
        if fact is None:
            skipped += 1
        else:
            rows.append(fact)

    rows.sort(key=lambda r: (
        _RANK[r["recurring"]], -float(r["approx_monthly"]), r["payee"], r["commodity"],
    ))
    return rows, skipped


def parse_args(argv):
    """(csv_path, today) from argv; raises SystemExit on misuse."""
    positional, today = [], date.today()
    it = iter(argv)
    for a in it:
        if a == "--today":
            try:
                today = parse_date(next(it))
            except StopIteration:
                raise SystemExit("error: --today needs a YYYY-MM-DD value")
        elif a.startswith("--today="):
            today = parse_date(a.split("=", 1)[1])
        elif a.startswith("--"):
            raise SystemExit(f"error: unknown option {a}")
        else:
            positional.append(a)
    if len(positional) != 1:
        raise SystemExit("usage: detect_recurring.py <csv_file> [--today YYYY-MM-DD]")
    return positional[0], today


def main(argv):
    csv_path, today = parse_args(argv)
    try:
        postings, drops = read_postings(csv_path)
    except FileNotFoundError:
        raise SystemExit(f"error: no such file: {csv_path}")
    except OSError as e:
        raise SystemExit(f"error: cannot read {csv_path}: {e}")

    dropped = (
        f"{drops['bad_date']} bad date, "
        f"{drops['no_payee_or_account']} missing payee/account, "
        f"{drops['no_positive_amount']} no positive amount"
    )
    if not postings:
        raise SystemExit(
            f"error: no usable expense postings in {csv_path} (rows dropped: "
            f"{dropped}). Expected the CSV from "
            'query report:"reg" account_pattern:"Expenses".'
        )

    rows, skipped = build_report(postings, today)
    print("# " + "\t".join(COLUMNS))
    for r in rows:
        print("\t".join(str(r[c]) for c in COLUMNS))
    print(
        f"# {skipped} group(s) dropped: seen in under 3 months and no clear "
        f"cadence yet; rows dropped while reading: {dropped}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
