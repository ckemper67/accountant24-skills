#!/usr/bin/env python3
"""Tests for compute_budget.py.

Run with: python3 -m unittest test_compute_budget -v   (from this directory)

Uses only the standard library (unittest), matching the script itself - the
vendored interpreter has no pip. Expected values are hand-computed from the
CSV fixtures each test builds, not re-derived by calling the script's own
parse_cell/main formulas, so a regression in the arithmetic actually fails a
test instead of being rubber-stamped by it.
"""
import csv
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import compute_budget as cb


class TempDirTestCase(unittest.TestCase):
    """A TemporaryDirectory scoped to the test, cleaned up via addCleanup -
    matches the pattern used by the other bundled skills' test suites
    (test_fetch_prices.py, test_modify_transactions.py), instead of
    NamedTemporaryFile(delete=False), which never gets cleaned up."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write_csv(self, rows: list[list[str]], name: str = "input.csv") -> str:
        path = Path(self._tmp.name) / name
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        return str(path)


# ---- parse_cell -------------------------------------------------------------


class TestParseCell(unittest.TestCase):
    def test_should_return_empty_dict_for_an_empty_string(self):
        self.assertEqual(cb.parse_cell(""), {})

    def test_should_return_empty_dict_for_a_bare_zero(self):
        self.assertEqual(cb.parse_cell("0"), {})

    def test_should_return_empty_dict_for_whitespace_only(self):
        self.assertEqual(cb.parse_cell("   "), {})

    def test_should_parse_a_single_signed_amount(self):
        self.assertEqual(cb.parse_cell("-123.13 USD"), {"USD": -123.13})

    def test_should_parse_a_positive_amount_with_no_sign(self):
        self.assertEqual(cb.parse_cell("356.42 EUR"), {"EUR": 356.42})

    def test_should_strip_thousands_separators(self):
        self.assertEqual(cb.parse_cell("1,234.56 EUR"), {"EUR": 1234.56})

    def test_should_parse_an_amount_with_no_decimal_part(self):
        self.assertEqual(cb.parse_cell("-123 USD"), {"USD": -123.0})

    def test_should_parse_multiple_currencies_in_one_cell(self):
        self.assertEqual(
            cb.parse_cell("-356.42 EUR, -607.87 USD"),
            {"EUR": -356.42, "USD": -607.87},
        )

    def test_should_sum_repeated_amounts_of_the_same_currency_in_one_cell(self):
        # Netting: two postings to the same account/period/currency must add,
        # not overwrite - this is the property the script's docstring claims.
        self.assertEqual(cb.parse_cell("-15.00 EUR, 15.00 EUR"), {"EUR": 0.0})

    def test_should_parse_a_5_letter_currency_code(self):
        self.assertEqual(cb.parse_cell("100.00 VTSAX"), {"VTSAX": 100.0})


# ---- main --------------------------------------------------------------


class TestMain(TempDirTestCase):
    def run_main(self, path: str) -> tuple[str, str, int]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                cb.main(path)
        except SystemExit as e:
            code = e.code or 0
        return out.getvalue(), err.getvalue(), code

    def test_should_error_when_fewer_than_2_period_columns(self):
        path = self.write_csv([["account", "2026-03"]])
        out, err, code = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertIn("need at least 2 period columns", err)
        self.assertEqual(out, "")

    def test_should_error_when_a_row_has_the_wrong_column_count(self):
        path = self.write_csv(
            [
                ["account", "2026-01", "2026-02", "2026-03"],
                ["Expenses:Groceries", "120.00 EUR", "135.00 EUR"],
            ]
        )
        out, err, code = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertIn("Expenses:Groceries", err)
        self.assertIn("expected 3", err)

    def test_should_skip_rows_with_an_empty_account_column(self):
        path = self.write_csv(
            [
                ["account", "2026-01", "2026-02", "2026-03"],
                ["", "0", "0", "0"],
                ["Expenses:Groceries", "100.00 EUR", "100.00 EUR", "50.00 EUR"],
            ]
        )
        out, _, _ = self.run_main(path)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Expenses:Groceries\t"))

    def test_should_skip_an_account_whose_cells_are_all_zero_or_empty(self):
        path = self.write_csv(
            [
                ["account", "2026-01", "2026-02", "2026-03"],
                ["Expenses:Unused", "0", "", "0"],
            ]
        )
        out, _, _ = self.run_main(path)
        self.assertEqual(out, "")

    def test_should_compute_total_average_and_current_for_a_single_currency(self):
        path = self.write_csv(
            [
                ["account", "2026-01", "2026-02", "2026-03"],
                ["Expenses:Groceries", "120.50 EUR", "135.00 EUR", "40.00 EUR"],
            ]
        )
        out, _, _ = self.run_main(path)
        # full months: 120.50 + 135.00 = 255.50, average = 127.75, current = 40.00
        self.assertEqual(
            out.strip(),
            "Expenses:Groceries\tEUR\t2\t255.50\t127.75\t40.00\t120.50,135.00",
        )

    def test_should_net_a_refund_instead_of_inflating_the_total(self):
        # 200.00 spend then a 50.00 refund in month 2 must average to
        # (200 - 50) / 2 = 75.00, not (200 + 50) / 2 = 125.00.
        path = self.write_csv(
            [
                ["account", "2026-01", "2026-02", "2026-03"],
                ["Expenses:Gear", "200.00 EUR", "-50.00 EUR", "0"],
            ]
        )
        out, _, _ = self.run_main(path)
        self.assertEqual(
            out.strip(),
            "Expenses:Gear\tEUR\t2\t150.00\t75.00\t0.00\t200.00,-50.00",
        )

    def test_should_emit_one_line_per_currency_sorted_alphabetically(self):
        path = self.write_csv(
            [
                ["account", "2026-01", "2026-02", "2026-03"],
                [
                    "Expenses:Travel",
                    "120.00 EUR, 50.00 USD",
                    "80.00 EUR",
                    "10.00 USD",
                ],
            ]
        )
        out, _, _ = self.run_main(path)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        # EUR: months [120.00, 80.00], total 200.00, avg 100.00, no USD in current cell -> current 0.00
        self.assertEqual(
            lines[0],
            "Expenses:Travel\tEUR\t2\t200.00\t100.00\t0.00\t120.00,80.00",
        )
        # USD: months [50.00, 0.00], total 50.00, avg 25.00, current 10.00
        self.assertEqual(
            lines[1],
            "Expenses:Travel\tUSD\t2\t50.00\t25.00\t10.00\t50.00,0.00",
        )

    def test_should_average_over_the_actual_full_month_count_not_a_hardcoded_12(self):
        path = self.write_csv(
            [
                ["account", "2026-01", "2026-02", "2026-03", "2026-04"],
                ["Expenses:Rent", "1000.00 EUR", "1000.00 EUR", "1000.00 EUR", "1000.00 EUR"],
            ]
        )
        out, _, _ = self.run_main(path)
        # 3 full months (2026-01..03), current = 2026-04
        self.assertEqual(
            out.strip(),
            "Expenses:Rent\tEUR\t3\t3000.00\t1000.00\t1000.00\t1000.00,1000.00,1000.00",
        )


# ---- CLI entry point ---------------------------------------------------------


class TestScriptEntryPoint(TempDirTestCase):
    """A real subprocess invocation - exercises `if __name__ == "__main__"`
    itself, which importing the module for the tests above never runs."""

    def script_path(self) -> Path:
        return Path(__file__).resolve().parent / "compute_budget.py"

    def test_should_exit_1_and_print_usage_with_no_arguments(self):
        proc = subprocess.run(
            [sys.executable, str(self.script_path())], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("usage:", proc.stderr)

    def test_should_exit_1_and_print_usage_with_too_many_arguments(self):
        proc = subprocess.run(
            [sys.executable, str(self.script_path()), "a.csv", "extra"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("usage:", proc.stderr)

    def test_should_print_aggregated_output_for_a_real_invocation(self):
        path = self.write_csv(
            [
                ["account", "2026-01", "2026-02", "2026-03"],
                ["Expenses:Groceries", "100.00 EUR", "100.00 EUR", "20.00 EUR"],
            ]
        )
        proc = subprocess.run(
            [sys.executable, str(self.script_path()), path], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(
            proc.stdout.strip(),
            "Expenses:Groceries\tEUR\t2\t200.00\t100.00\t20.00\t100.00,100.00",
        )


if __name__ == "__main__":
    unittest.main()
