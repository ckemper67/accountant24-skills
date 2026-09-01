#!/usr/bin/env python3
"""Tests for map_payees.py.

Run with: python3 -m unittest test_map_payees -v   (from this directory)

Standard library only (unittest), matching the script - the vendored
interpreter has no pip. Expected values are hand-computed from each fixture,
not re-derived from the script's own logic.
"""
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import map_payees as mp

HEADER = ["txnidx", "date", "code", "description", "account", "amount", "total"]


def tsv(rows, header=HEADER):
    """A tab-separated string, hledger-style (unquoted). rows: list of lists."""
    lines = ["\t".join(header)] + ["\t".join(r) for r in rows]
    return "\n".join(lines) + "\n"


# ---- payee_of ------------------------------------------------------------


class TestPayeeOf(unittest.TestCase):
    def test_should_take_text_before_the_first_pipe(self):
        self.assertEqual(mp.payee_of("Netflix | monthly plan"), "Netflix")

    def test_should_return_the_whole_string_when_there_is_no_pipe(self):
        self.assertEqual(mp.payee_of("Corner Shop"), "Corner Shop")

    def test_should_strip_surrounding_whitespace(self):
        self.assertEqual(mp.payee_of("  Spotify  | Premium"), "Spotify")

    def test_should_split_on_only_the_first_pipe(self):
        self.assertEqual(mp.payee_of("A | b | c"), "A")


# ---- main --------------------------------------------------------------


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write_tsv(self, rows, name="reg.tsv", header=HEADER):
        path = Path(self._tmp.name) / name
        path.write_text(tsv(rows, header), encoding="utf-8")
        return str(path)

    def run_main(self, path):
        out, err, code = StringIO(), StringIO(), 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                mp.main(path)
        except SystemExit as e:
            code = e.code or 0
        return out.getvalue(), err.getvalue(), code


class TestMain(TempDirTestCase):
    def test_should_emit_one_line_per_payee_with_its_account_and_count(self):
        path = self.write_tsv([
            ["1", "2025-01-01", "", "Netflix", "Expenses:Subscriptions", "10 EUR", ""],
            ["2", "2025-02-01", "", "Netflix", "Expenses:Subscriptions", "10 EUR", ""],
        ])
        out, _err, code = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertEqual(out.rstrip("\n"), "\tNetflix\tExpenses:Subscriptions (2)")

    def test_should_flag_a_payee_posting_to_more_than_one_account_as_multi(self):
        path = self.write_tsv([
            ["1", "2025-01-05", "", "Shell", "Expenses:Transport:Fuel", "50 EUR", ""],
            ["2", "2025-02-05", "", "Shell", "Expenses:Transport:Fuel", "52 EUR", ""],
            ["3", "2025-02-06", "", "Shell", "Expenses:Groceries", "8 EUR", ""],
        ])
        out, _err, _code = self.run_main(path)
        self.assertEqual(
            out.rstrip("\n"),
            "MULTI\tShell\tExpenses:Transport:Fuel (2), Expenses:Groceries (1)",
        )

    def test_should_not_flag_a_single_account_payee_even_with_many_postings(self):
        path = self.write_tsv([
            ["1", "2025-01-01", "", "Rent", "Expenses:Rent", "900 EUR", ""],
            ["2", "2025-02-01", "", "Rent", "Expenses:Rent", "900 EUR", ""],
            ["3", "2025-03-01", "", "Rent", "Expenses:Rent", "900 EUR", ""],
        ])
        out, _err, _code = self.run_main(path)
        self.assertEqual(out.rstrip("\n"), "\tRent\tExpenses:Rent (3)")

    def test_should_rank_accounts_by_count_then_name(self):
        rows = (
            [["a", "2025-01-01", "", "P", "Expenses:B", "1 EUR", ""]]
            + [["b", "2025-01-02", "", "P", "Expenses:A", "1 EUR", ""]] * 3
            + [["c", "2025-01-03", "", "P", "Expenses:C", "1 EUR", ""]]
        )
        out, _err, _code = self.run_main(self.write_tsv(rows))
        # Expenses:A has 3, then B and C tie at 1 and sort by name
        self.assertEqual(
            out.rstrip("\n"),
            "MULTI\tP\tExpenses:A (3), Expenses:B (1), Expenses:C (1)",
        )

    def test_should_sort_payees_case_insensitively(self):
        path = self.write_tsv([
            ["1", "2025-01-01", "", "zebra", "Expenses:X", "1 EUR", ""],
            ["2", "2025-01-02", "", "Apple", "Expenses:Y", "1 EUR", ""],
        ])
        out, _err, _code = self.run_main(path)
        payees = [ln.split("\t")[1] for ln in out.rstrip("\n").splitlines()]
        self.assertEqual(payees, ["Apple", "zebra"])

    def test_should_use_the_payee_part_of_a_piped_description(self):
        path = self.write_tsv([
            ["1", "2025-01-01", "", "EDEKA | groceries run", "Expenses:Groceries", "20 EUR", ""],
        ])
        out, _err, _code = self.run_main(path)
        self.assertEqual(out.rstrip("\n"), "\tEDEKA\tExpenses:Groceries (1)")

    def test_should_keep_a_quote_character_in_a_payee_name(self):
        # hledger tsv is unquoted; the QUOTE_NONE reader must not strip these.
        path = self.write_tsv([
            ["1", "2025-01-01", "", '"Ye Olde" Shoppe', "Expenses:Misc", "5 EUR", ""],
        ])
        out, _err, _code = self.run_main(path)
        self.assertEqual(out.rstrip("\n"), '\t"Ye Olde" Shoppe\tExpenses:Misc (1)')

    def test_should_skip_rows_with_an_empty_payee_or_account(self):
        path = self.write_tsv([
            ["1", "2025-01-01", "", "", "Expenses:Misc", "5 EUR", ""],
            ["2", "2025-01-02", "", "Shop", "", "5 EUR", ""],
            ["3", "2025-01-03", "", "Real", "Expenses:Real", "5 EUR", ""],
        ])
        out, _err, _code = self.run_main(path)
        self.assertEqual(out.rstrip("\n"), "\tReal\tExpenses:Real (1)")

    def test_should_error_when_a_required_column_is_missing(self):
        path = self.write_tsv(
            [["Netflix", "Expenses:Subscriptions"]],
            header=["payee", "acct"],
        )
        out, err, code = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertIn("missing column(s)", err)
        self.assertIn("account", err)
        self.assertIn("description", err)
        self.assertEqual(out, "")

    def test_should_produce_no_output_for_a_header_only_file(self):
        out, _err, code = self.run_main(self.write_tsv([]))
        self.assertEqual(code, 0)
        self.assertEqual(out.rstrip("\n"), "")


# ---- CLI entry point --------------------------------------------------


class TestScriptEntryPoint(TempDirTestCase):
    def script_path(self):
        return Path(__file__).resolve().parent / "map_payees.py"

    def test_should_exit_1_with_usage_when_given_no_arguments(self):
        proc = subprocess.run([sys.executable, str(self.script_path())], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("usage:", proc.stderr)

    def test_should_run_end_to_end_for_a_real_invocation(self):
        path = self.write_tsv([
            ["1", "2025-01-01", "", "Netflix", "Expenses:Subscriptions", "10 EUR", ""],
        ])
        proc = subprocess.run(
            [sys.executable, str(self.script_path()), path], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.rstrip("\n"), "\tNetflix\tExpenses:Subscriptions (1)")


if __name__ == "__main__":
    unittest.main()
