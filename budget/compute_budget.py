#!/usr/bin/env python3
"""Aggregate an hledger `bal ... --period monthly -O csv --invert` dump into
per-account, per-currency monthly figures.

Usage: python3 compute_budget.py <csv_file>

Input: the CSV hledger emits for a multi-period balance report - a header row
of "account" then one column per period (e.g. "2025-07", "2025-08", ...), and
one data row per account. A cell holds one or more signed amounts, e.g.
"0", "-123.13 USD", or "-356.42 EUR, -607.87 USD" for a multi-currency
posting in that account/period.

The last period column is always treated as the current, still-in-progress
month and excluded from every average - it is reported separately so the
model can show "so far this month" context without it skewing the budget.

Prints one line per account/currency, tab-separated:
  account, currency, full_month_count, total, average, current_month,
  then the per-month values in order (comma-separated), so the model can see
  the actual monthly trend and decide what counts as an outlier or a
  seasonal/annual pattern - that judgment call is deliberately left to the
  model, not this script. Averages are computed by netting each month's
  already-inverted signed value (not summing absolute values), so a refund
  or credit correctly reduces the total instead of adding to it.
"""
import csv
import re
import sys
from collections import defaultdict

AMOUNT_RE = re.compile(r"(-?[\d,]+\.?\d*)\s*([A-Za-z]{2,5})")


def parse_cell(cell: str) -> dict[str, float]:
    amounts: dict[str, float] = {}
    cell = cell.strip()
    if not cell or cell == "0":
        return amounts
    for amt_str, currency in AMOUNT_RE.findall(cell):
        amounts[currency] = amounts.get(currency, 0.0) + float(amt_str.replace(",", ""))
    return amounts


def main(path: str) -> None:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        periods = header[1:]
        if len(periods) < 2:
            print("error: need at least 2 period columns (1+ full month, 1 current)", file=sys.stderr)
            sys.exit(1)
        full_periods = periods[:-1]

        rows = [(row[0], row[1:]) for row in reader if row and row[0]]

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
            average = total / len(full_periods) if full_periods else 0.0
            current = current_by_currency.get(currency, 0.0)
            monthly_str = ",".join(f"{v:.2f}" for v in monthly)
            print(f"{account}\t{currency}\t{len(full_periods)}\t{total:.2f}\t{average:.2f}\t{current:.2f}\t{monthly_str}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: compute_budget.py <csv_file>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
