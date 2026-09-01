#!/usr/bin/env python3
"""Aggregate an hledger `bal ... --period monthly -O csv` dump into
per-account, per-currency monthly figures.

Usage: python3 compute_budget.py <csv_file>

Input: the CSV hledger emits for a multi-period balance report - a header row
of "account" then one column per period (e.g. "2025-07", "2025-08", ...), and
one data row per account. Expense balances come out positive (no `--invert`);
a cell holds one or more signed amounts, e.g. "0", "123.13 USD", "$123.13",
"1.234,56 EUR", or "356.42 EUR, 607.87 USD" for a multi-currency posting in
that account/period. A negative cell is a net refund/credit month and is kept
as-is. hledger's trailing "total" row is ignored.

Amounts: either ',' or '.' may be the decimal mark (hledger follows the
journal's `decimal-mark`); "1,234.56" and "1.234,56" both parse to 1234.56.
A lone separator with exactly 3 trailing digits is read as a thousands
separator. A commodity may be a 2-5 letter code (before or after the number)
or a leading "$".

The last period column is always treated as the current, still-in-progress
month and excluded from every average - it is reported separately so the
model can show "so far this month" context without it skewing the budget.

Prints one line per account/currency, tab-separated:
  account, currency, full_month_count, total, average, current_month,
  then the per-month values in order (comma-separated), so the model can see
  the actual monthly trend and decide what counts as an outlier or a
  seasonal/annual pattern - that judgment call is deliberately left to the
  model, not this script. Averages are computed by netting each month's
  signed value (not summing absolute values), so a refund or credit - which
  lands as a negative amount in that month's cell - correctly reduces the
  total instead of adding to it.
"""
import csv
import re
import sys
from collections import defaultdict

# One amount: an optional leading '-', an optional commodity ("$" or a 2-5
# letter code, before or after), and a number that may carry ',' / '.' group
# and decimal marks.
AMOUNT_RE = re.compile(
    r"(?P<lead>-)?\s*"
    r"(?P<pre>[A-Za-z]{2,5}|\$)?\s*"
    r"(?P<num>-?\d[\d.,]*\d|-?\d)"
    r"(?:\s*(?P<post>[A-Za-z]{2,5}))?"
)


def parse_number(text: str):
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


def parse_cell(cell: str) -> dict[str, float]:
    amounts: dict[str, float] = {}
    cell = cell.strip()
    if not cell or cell == "0":
        return amounts
    for m in AMOUNT_RE.finditer(cell):
        num = m.group("num")
        if m.group("lead") == "-" and not num.startswith("-"):
            num = "-" + num
        # AMOUNT_RE always captures at least one digit, so parse_number never
        # returns None here.
        value = parse_number(num)
        currency = m.group("pre") or m.group("post") or "?"
        amounts[currency] = amounts.get(currency, 0.0) + value
    return amounts


def main(path: str) -> None:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or len(header) < 2:
            print("error: empty CSV or no period columns", file=sys.stderr)
            sys.exit(1)
        periods = header[1:]
        if len(periods) < 2:
            print("error: need at least 2 period columns (1+ full month, 1 current)", file=sys.stderr)
            sys.exit(1)
        full_periods = periods[:-1]

        # Skip hledger's trailing "total" summary row - it is not an account.
        rows = [
            (row[0], row[1:])
            for row in reader
            if row and row[0] and row[0].strip().lower() != "total"
        ]

    for account, cells in rows:
        if len(cells) != len(periods):
            print(f"error: row for {account!r} has {len(cells)} columns, expected {len(periods)}", file=sys.stderr)
            sys.exit(1)

        by_currency: dict[str, list[float]] = defaultdict(lambda: [0.0] * len(full_periods))
        current_by_currency: dict[str, float] = defaultdict(float)

        for i, cell in enumerate(cells):
            parsed = parse_cell(cell)
            for currency, amount in parsed.items():
                if i < len(full_periods):
                    by_currency[currency][i] = amount
                else:
                    current_by_currency[currency] += amount

        currencies = set(by_currency) | set(current_by_currency)
        for currency in sorted(currencies):
            monthly = by_currency.get(currency, [0.0] * len(full_periods))
            total = sum(monthly)
            average = total / len(full_periods)
            current = current_by_currency.get(currency, 0.0)
            monthly_str = ",".join(f"{v:.2f}" for v in monthly)
            print(f"{account}\t{currency}\t{len(full_periods)}\t{total:.2f}\t{average:.2f}\t{current:.2f}\t{monthly_str}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: compute_budget.py <csv_file>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
