#!/usr/bin/env python3
"""Tests for audit_ledger.py.

Run with: python3 -m unittest test_audit_ledger -v   (from this directory)

Standard library only (unittest), matching the script - the vendored
interpreter has no pip. Expected findings are hand-built from each fixture, not
re-derived from the script's own logic. Checks are exercised by handing them
transaction dicts and a Findings recorder directly; the hledger-invoking layer
is covered by a few end-to-end runs over tiny fixture journals (hledger is on
PATH wherever the `query`/`validate` tools work).
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

import audit_ledger as al


# ---- helpers ---------------------------------------------------------------


def txn(dt, desc, postings, tags=None, date2=None, status="*"):
    """A transaction dict in the shape parse_print produces.
    `postings` is a list of (account, amount) or (account, amount, {tags})."""
    ps = []
    for p in postings:
        acct, amt = p[0], p[1]
        ps.append(
            {
                "account": acct,
                "amount": amt,
                "commodity": "USD" if amt is not None else "",
                "has_assertion": len(p) > 2 and p[2].get("assertion", False),
                "tags": (p[2] if len(p) > 2 else {}) or {},
            }
        )
    return {
        "date": dt,
        "date2": date2,
        "status": status,
        "code": "",
        "description": desc,
        "payee": desc.split("|", 1)[0].strip(),
        "tags": tags or {},
        "postings": ps,
    }


def rows_of(f):
    """Findings as a list of (severity, check, scope, count, detail)."""
    return list(f._rows)


def checks_in(f):
    return sorted({r[1] for r in f._rows})


HLEDGER = shutil.which("hledger")


# ---- parse_amount --------------------------------------------------------------


class TestParseAmount(unittest.TestCase):
    def test_should_parse_us_style_with_trailing_commodity(self):
        self.assertEqual(al.parse_amount("-1,234.56 USD"), (-1234.56, "USD"))

    def test_should_parse_eu_style_with_trailing_commodity(self):
        self.assertEqual(al.parse_amount("-1.234,56 EUR"), (-1234.56, "EUR"))

    def test_should_parse_commodity_before_number(self):
        self.assertEqual(al.parse_amount("USD -50"), (-50.0, "USD"))

    def test_should_parse_bare_dollar_sign(self):
        self.assertEqual(al.parse_amount("$-10.00"), (-10.0, "$"))

    def test_should_ignore_cost_after_at_sign(self):
        # Only the primary amount matters for an audit.
        self.assertEqual(al.parse_amount("10 DFTCX @ 12.34 USD"), (10.0, "DFTCX"))

    def test_should_treat_lone_comma_with_two_decimals_as_decimal_point(self):
        self.assertEqual(al.parse_amount("1234,56 EUR"), (1234.56, "EUR"))

    def test_should_treat_lone_comma_as_thousands_separator_otherwise(self):
        self.assertEqual(al.parse_amount("1,234 USD"), (1234.0, "USD"))

    def test_should_return_none_for_empty_or_nonnumeric(self):
        self.assertIsNone(al.parse_amount(""))
        self.assertIsNone(al.parse_amount(None))
        self.assertIsNone(al.parse_amount("   "))


class TestParseBalanceCell(unittest.TestCase):
    def test_should_return_empty_for_a_zero_cell(self):
        self.assertEqual(al.parse_balance_cell("0"), [])

    def test_should_split_a_multicommodity_cell_on_newlines(self):
        self.assertEqual(
            al.parse_balance_cell("1,000.00 USD\n-20.00 EUR"),
            [(1000.0, "USD"), (-20.0, "EUR")],
        )


class TestNormPayee(unittest.TestCase):
    def test_should_fold_case_and_strip_non_alphanumerics(self):
        self.assertEqual(al.norm_payee("NETFLIX.COM *sub"), al.norm_payee("Netflix com sub"))

    def test_should_reduce_to_empty_string_for_punctuation_only(self):
        self.assertEqual(al.norm_payee("--- / ---"), "")


# ---- parse_print -------------------------------------------------------------


PRINT_SAMPLE = """\
2026-08-18 * Hummels Brauhaus | dinner
    ; fitid: 20260818090085
    ; import_id: 84e3625c80429d27
    liabilities:credit-card:citi     -63.89 USD
    expenses:food:dining-out          63.89 USD

2026-08-15 * Balance Assertion
    liabilities:credit-card:chase    0.00 USD = -109.23 USD

2024-01-03=2024-01-05 ! External Withdrawal WF | ACH
    assets:bank:bayfed              -1981.53 USD
    liabilities:mortgage            1981.53 USD  ; note: principal only
"""


class TestParsePrint(unittest.TestCase):
    def setUp(self):
        self.txns = al.parse_print(PRINT_SAMPLE)

    def test_should_parse_every_transaction(self):
        self.assertEqual(len(self.txns), 3)

    def test_should_split_payee_from_description(self):
        self.assertEqual(self.txns[0]["payee"], "Hummels Brauhaus")

    def test_should_collect_transaction_level_tags(self):
        self.assertEqual(self.txns[0]["tags"]["fitid"], "20260818090085")
        self.assertEqual(self.txns[0]["tags"]["import_id"], "84e3625c80429d27")

    def test_should_parse_posting_accounts_and_amounts(self):
        p = self.txns[0]["postings"]
        self.assertEqual(p[0]["account"], "liabilities:credit-card:citi")
        self.assertEqual(p[0]["amount"], -63.89)
        self.assertEqual(p[1]["amount"], 63.89)

    def test_should_flag_a_posting_that_carries_a_balance_assertion(self):
        self.assertTrue(self.txns[1]["postings"][0]["has_assertion"])

    def test_should_capture_secondary_date_and_status(self):
        self.assertEqual(self.txns[2]["date"], "2024-01-03")
        self.assertEqual(self.txns[2]["date2"], "2024-01-05")
        self.assertEqual(self.txns[2]["status"], "!")

    def test_should_read_posting_level_tags(self):
        self.assertEqual(self.txns[2]["postings"][1]["tags"].get("note"), "principal only")


# ---- classify / account directives ------------------------------------------


class TestClassify(unittest.TestCase):
    DIRECTIVES = {
        "liabilities:loan:car": {"type": "A"},  # deliberately mis-declared
        "assets:bank:checking": {"type": None},
    }

    def test_should_prefer_an_explicit_type_directive_over_the_name(self):
        self.assertEqual(al.classify("liabilities:loan:car", self.DIRECTIVES), "asset")

    def test_should_fall_back_to_the_name_prefix_when_type_is_absent(self):
        self.assertEqual(al.classify("assets:bank:checking", self.DIRECTIVES), "asset")

    def test_should_fall_back_to_the_name_prefix_for_an_undeclared_account(self):
        self.assertEqual(al.classify("expenses:food", self.DIRECTIVES), "expense")
        self.assertEqual(al.classify("income:salary", self.DIRECTIVES), "income")

    def test_should_return_empty_for_an_unclassifiable_name(self):
        self.assertEqual(al.classify("weird:thing", {}), "")


class TestParseAccountDirectives(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")
        return str(self.root / name)

    def test_should_follow_include_directives_and_read_types(self):
        self.write(
            "accounts.journal",
            "account assets:bank:checking\n    type:A\n"
            "account liabilities:mortgage\n    type:L\n"
            "account expenses:food\n",  # no type
        )
        main = self.write("main.journal", "include accounts.journal\n")
        got = al.parse_account_directives(main)
        self.assertEqual(got["assets:bank:checking"]["type"], "A")
        self.assertEqual(got["liabilities:mortgage"]["type"], "L")
        self.assertIsNone(got["expenses:food"]["type"])

    def test_should_read_a_type_in_a_trailing_comment_on_the_account_line(self):
        main = self.write("main.journal", "account assets:cash  ; type:A\n")
        got = al.parse_account_directives(main)
        self.assertEqual(got["assets:cash"]["type"], "A")


# ---- individual checks -----------------------------------------------------


class TestEquityPlugs(unittest.TestCase):
    def run_check(self, txns, first_seen=None):
        f = al.Findings()
        explained = {}
        al.check_equity_plugs(txns, {}, first_seen or {}, explained, f)
        return f, explained

    def test_should_flag_a_two_posting_transaction_offset_to_the_plug(self):
        t = txn("2021-05-01", "EDEKA", [("expenses:uncategorized", 40.0), (al.EQUITY_PLUG, -40.0)])
        f, explained = self.run_check([t])
        self.assertEqual(checks_in(f), ["equity-plug"])
        self.assertEqual(rows_of(f)[0][3], 1)  # count
        self.assertIn("expenses:uncategorized", explained)

    def test_should_aggregate_per_real_account(self):
        ts = [
            txn("2021-05-01", "A", [("assets:bank:x", 10.0), (al.EQUITY_PLUG, -10.0)]),
            txn("2021-06-01", "B", [("assets:bank:x", 20.0), (al.EQUITY_PLUG, -20.0)]),
        ]
        f, _ = self.run_check(ts)
        self.assertEqual(len(rows_of(f)), 1)
        self.assertEqual(rows_of(f)[0][3], 2)

    def test_should_not_flag_the_accounts_genuine_opening_balance(self):
        t = txn("2020-01-01", "Opening", [("assets:bank:x", 500.0), (al.EQUITY_PLUG, -500.0)])
        f, _ = self.run_check([t], first_seen={"assets:bank:x": "2020-01-01"})
        self.assertEqual(rows_of(f), [])

    def test_should_leave_reconciliation_lumps_to_the_other_check(self):
        t = txn("2026-08-20", "Citi balance correction per OFX",
                [("liabilities:credit-card:citi", -1000.0), (al.EQUITY_PLUG, 1000.0)])
        f, _ = self.run_check([t])
        self.assertEqual(rows_of(f), [])

    def test_should_ignore_an_ordinary_two_account_transaction(self):
        t = txn("2021-05-01", "EDEKA", [("expenses:food", 40.0), ("assets:bank:x", -40.0)])
        f, _ = self.run_check([t])
        self.assertEqual(rows_of(f), [])


class TestLumpAdjustments(unittest.TestCase):
    def run_check(self, txns, first_seen):
        f = al.Findings()
        explained = {}
        al.check_lump_adjustments(txns, first_seen, explained, f)
        return f, explained

    def test_should_flag_a_described_correction_far_from_account_open(self):
        t = txn("2026-08-20", "Citi balance correction per OFX 2026-08-20",
                [("liabilities:credit-card:citi", -11956.59), (al.EQUITY_PLUG, 11956.59)])
        f, explained = self.run_check([t], {"liabilities:credit-card:citi": "2019-01-01"})
        self.assertEqual(checks_in(f), ["late-lump-adjustment"])
        self.assertIn("liabilities:credit-card:citi", explained)

    def test_should_ignore_a_correction_that_is_the_opening_balance(self):
        t = txn("2019-01-05", "Opening balance adjustment",
                [("assets:bank:x", 500.0), (al.EQUITY_PLUG, -500.0)])
        f, _ = self.run_check([t], {"assets:bank:x": "2019-01-01"})
        self.assertEqual(rows_of(f), [])

    def test_should_ignore_a_plain_transaction_with_no_correction_wording(self):
        t = txn("2026-08-20", "Some Vendor",
                [("liabilities:credit-card:citi", -11956.59), (al.EQUITY_PLUG, 11956.59)])
        f, _ = self.run_check([t], {"liabilities:credit-card:citi": "2019-01-01"})
        self.assertEqual(rows_of(f), [])

    def test_should_ignore_a_correction_below_the_size_floor(self):
        t = txn("2026-08-20", "small balance correction",
                [("liabilities:credit-card:citi", -10.0), (al.EQUITY_PLUG, 10.0)])
        f, _ = self.run_check([t], {"liabilities:credit-card:citi": "2019-01-01"})
        self.assertEqual(rows_of(f), [])


class TestBrokenChains(unittest.TestCase):
    def run_check(self, txns):
        f = al.Findings()
        al.check_broken_chains(txns, f)
        return f

    def test_should_pair_a_bank_to_equity_leg_with_an_equity_to_card_leg(self):
        ts = [
            txn("2026-07-01", "Payment", [("assets:bank:checking", -300.0), (al.EQUITY_PLUG, 300.0)]),
            txn("2026-07-03", "Payment", [(al.EQUITY_PLUG, -300.0), ("liabilities:credit-card:citi", 300.0)]),
        ]
        f = self.run_check(ts)
        self.assertEqual(checks_in(f), ["broken-chain"])

    def test_should_not_pair_when_the_dates_are_far_apart(self):
        ts = [
            txn("2026-07-01", "Payment", [("assets:bank:checking", -300.0), (al.EQUITY_PLUG, 300.0)]),
            txn("2026-08-01", "Payment", [(al.EQUITY_PLUG, -300.0), ("liabilities:credit-card:citi", 300.0)]),
        ]
        self.assertEqual(rows_of(self.run_check(ts)), [])

    def test_should_skip_a_transaction_marked_pending(self):
        ts = [
            txn("2026-07-01", "Payment", [("assets:bank:checking", -300.0), (al.EQUITY_PLUG, 300.0)],
                tags={"pending": "x"}),
            txn("2026-07-03", "Payment", [(al.EQUITY_PLUG, -300.0), ("liabilities:credit-card:citi", 300.0)]),
        ]
        self.assertEqual(rows_of(self.run_check(ts)), [])

    def test_should_not_pair_on_a_different_amount(self):
        ts = [
            txn("2026-07-01", "Payment", [("assets:bank:checking", -300.0), (al.EQUITY_PLUG, 300.0)]),
            txn("2026-07-03", "Payment", [(al.EQUITY_PLUG, -250.0), ("liabilities:credit-card:citi", 250.0)]),
        ]
        self.assertEqual(rows_of(self.run_check(ts)), [])


class TestDuplicates(unittest.TestCase):
    def run_check(self, txns):
        f = al.Findings()
        al.check_duplicates(txns, f)
        return f

    def test_should_flag_two_transactions_sharing_an_import_id_as_high(self):
        ts = [
            txn("2026-01-01", "X", [("expenses:food", 30.0), ("assets:bank:x", -30.0)], tags={"import_id": "abc"}),
            txn("2026-01-09", "X", [("expenses:food", 30.0), ("assets:bank:x", -30.0)], tags={"import_id": "abc"}),
        ]
        f = self.run_check(ts)
        self.assertEqual(rows_of(f)[0][0], "high")
        self.assertEqual(rows_of(f)[0][1], "dup-id")

    def test_should_flag_a_same_day_same_amount_repeat_as_info(self):
        ts = [
            txn("2026-01-01", "Blue Bottle Coffee", [("expenses:food", 45.0), ("assets:bank:x", -45.0)]),
            txn("2026-01-01", "Blue Bottle Coffee", [("expenses:food", 45.0), ("assets:bank:x", -45.0)]),
        ]
        f = self.run_check(ts)
        self.assertIn("dup-fuzzy", checks_in(f))
        self.assertEqual([r for r in rows_of(f) if r[1] == "dup-fuzzy"][0][0], "info")

    def test_should_ignore_a_generic_bank_description(self):
        ts = [
            txn("2026-01-01", "Internal Transfer", [("assets:bank:x", 500.0), ("assets:bank:y", -500.0)]),
            txn("2026-01-01", "Internal Transfer", [("assets:bank:x", 500.0), ("assets:bank:y", -500.0)]),
        ]
        f = self.run_check(ts)
        self.assertNotIn("dup-fuzzy", checks_in(f))

    def test_should_ignore_a_small_same_day_repeat(self):
        ts = [
            txn("2026-01-01", "Blue Bottle Coffee", [("expenses:food", 6.0), ("assets:bank:x", -6.0)]),
            txn("2026-01-01", "Blue Bottle Coffee", [("expenses:food", 6.0), ("assets:bank:x", -6.0)]),
        ]
        self.assertNotIn("dup-fuzzy", checks_in(self.run_check(ts)))

    def test_should_not_flag_distinct_days(self):
        ts = [
            txn("2026-01-01", "Blue Bottle Coffee", [("expenses:food", 45.0), ("assets:bank:x", -45.0)]),
            txn("2026-01-02", "Blue Bottle Coffee", [("expenses:food", 45.0), ("assets:bank:x", -45.0)]),
        ]
        self.assertNotIn("dup-fuzzy", checks_in(self.run_check(ts)))


class TestLoanAmortisation(unittest.TestCase):
    def run_check(self, txns, first_seen=None):
        f = al.Findings()
        al.check_loan_amortisation(txns, first_seen or {}, f)
        return f

    def test_should_flag_principal_payments_with_no_interest_posting(self):
        ts = [
            txn("2024-01-01", "Draw", [("liabilities:mortgage", -100000.0), ("assets:bank:x", 100000.0)]),
            txn("2024-02-01", "Pay", [("liabilities:mortgage", 1000.0), ("assets:bank:x", -1000.0)]),
            txn("2024-03-01", "Pay", [("liabilities:mortgage", 1000.0), ("assets:bank:x", -1000.0)]),
        ]
        f = self.run_check(ts)
        row = [r for r in rows_of(f) if r[1] == "no-interest-split"][0]
        self.assertEqual(row[3], 2)

    def test_should_not_flag_a_payment_that_splits_out_interest(self):
        ts = [
            txn("2024-01-01", "Draw", [("liabilities:mortgage", -100000.0), ("assets:bank:x", 100000.0)]),
            txn("2024-02-01", "Pay", [
                ("liabilities:mortgage", 700.0),
                ("expenses:interest:mortgage", 300.0),
                ("assets:bank:x", -1000.0),
            ]),
        ]
        f = self.run_check(ts)
        self.assertNotIn("no-interest-split", checks_in(f))

    def test_should_recognise_a_hyphen_suffixed_interest_account(self):
        # expenses:financial:mortgage-interest is a real chart-of-accounts name.
        ts = [
            txn("2024-01-01", "Draw", [("liabilities:mortgage", -100000.0), ("assets:bank:x", 100000.0)]),
            txn("2024-02-01", "Pay", [
                ("liabilities:mortgage", 700.0),
                ("expenses:financial:mortgage-interest", 300.0),
                ("assets:bank:x", -1000.0),
            ]),
        ]
        self.assertNotIn("no-interest-split", checks_in(self.run_check(ts)))

    def test_should_note_when_a_plug_entry_masks_the_missing_origination(self):
        ts = [
            txn("2026-08-02", "Wf Home Mtg balance correction per statement",
                [("liabilities:mortgage", -23778.36), ("equity:opening-balances", 23778.36)]),
            txn("2026-09-01", "Pay", [("liabilities:mortgage", 700.0), ("assets:bank:x", -700.0)]),
        ]
        f = self.run_check(ts, first_seen={"liabilities:mortgage": "2026-08-02"})
        row = [r for r in rows_of(f) if r[1] == "no-loan-origination"][0]
        self.assertIn("masked", row[4])

    def test_should_flag_a_loan_with_no_origination_entry(self):
        ts = [txn("2024-02-01", "Pay", [("liabilities:loan:car", 300.0), ("assets:bank:x", -300.0)])]
        f = self.run_check(ts, first_seen={"liabilities:loan:car": "2024-02-01"})
        self.assertIn("no-loan-origination", checks_in(f))


class TestAssertionsCoverage(unittest.TestCase):
    def run_check(self, txns, today):
        f = al.Findings()
        al.check_assertions_coverage(txns, {}, today, f)
        return f

    def test_should_flag_an_active_asset_account_with_no_assertion_ever(self):
        ts = [txn("2026-08-20", "Buy", [("assets:bank:x", -5.0), ("expenses:food", 5.0)])]
        f = self.run_check(ts, date(2026, 9, 1))
        self.assertIn("no-assertion", checks_in(f))

    def test_should_not_flag_an_account_that_has_an_assertion(self):
        ts = [
            txn("2026-08-20", "Buy", [("assets:bank:x", -5.0), ("expenses:food", 5.0)]),
            txn("2026-08-21", "Assert", [("assets:bank:x", 0.0, {"assertion": True})]),
        ]
        f = self.run_check(ts, date(2026, 9, 1))
        self.assertEqual(rows_of(f), [])

    def test_should_not_flag_a_dormant_account(self):
        ts = [txn("2020-01-01", "Buy", [("assets:bank:x", -5.0), ("expenses:food", 5.0)])]
        f = self.run_check(ts, date(2026, 9, 1))
        self.assertEqual(rows_of(f), [])


class TestDescDates(unittest.TestCase):
    def test_should_flag_a_description_date_far_from_the_transaction_date(self):
        f = al.Findings()
        al.check_desc_dates([txn("2024-02-18", "Netflix charge 2024-03-15",
                                 [("expenses:subscriptions", 10.0), ("assets:bank:x", -10.0)])], f)
        self.assertIn("date-in-desc", checks_in(f))

    def test_should_not_flag_a_close_description_date(self):
        f = al.Findings()
        al.check_desc_dates([txn("2024-03-13", "Autopay 03/15/24",
                                 [("expenses:x", 10.0), ("assets:bank:y", -10.0)])], f)
        self.assertNotIn("date-in-desc", checks_in(f))

    def test_should_note_when_no_secondary_dates_are_used_at_all(self):
        f = al.Findings()
        al.check_desc_dates([txn("2024-01-01", "X", [("a:b", 1.0), ("c:d", -1.0)])], f)
        self.assertTrue(any("no secondary dates" in n for n in f._notes))

    def test_should_not_emit_that_note_when_a_secondary_date_exists(self):
        f = al.Findings()
        al.check_desc_dates([txn("2024-01-01", "X", [("a:b", 1.0), ("c:d", -1.0)], date2="2024-01-03")], f)
        self.assertFalse(any("no secondary dates" in n for n in f._notes))


class TestMultiCard(unittest.TestCase):
    def test_should_report_each_card_span_and_a_multi_month_gap(self):
        ts = [
            txn("2022-01-05", "Shop", [("liabilities:credit-card:citi", -10.0), ("expenses:x", 10.0)]),
            txn("2022-02-05", "Shop", [("liabilities:credit-card:citi", -10.0), ("expenses:x", 10.0)]),
            txn("2023-06-05", "Shop", [("liabilities:credit-card:citi", -10.0), ("expenses:x", 10.0)]),
        ]
        f = al.Findings()
        al.check_multi_card(ts, {}, f)
        row = [r for r in rows_of(f) if r[1] == "card-span"][0]
        self.assertIn("possible card reissue", row[4])


class TestFindingsRender(unittest.TestCase):
    def test_should_order_high_before_warn_before_info(self):
        f = al.Findings()
        f.add("info", "c", "s", 1, "d")
        f.add("high", "a", "s", 1, "d")
        f.add("warn", "b", "s", 1, "d")
        body = f.render()
        self.assertLess(body.index("\nhigh\t"), body.index("\nwarn\t"))
        self.assertLess(body.index("\nwarn\t"), body.index("\ninfo\t"))

    def test_should_summarise_counts_in_the_header(self):
        f = al.Findings()
        f.add("high", "a", "s", 1, "d")
        f.add("high", "a", "s", 1, "d")
        self.assertIn("2 high", f.render().splitlines()[0])


# ---- end to end over a fixture journal -------------------------------------


CLEAN_JOURNAL = """\
account assets:bank:checking
    type:A
account expenses:food
    type:X
account equity:opening-balances
    type:E

2026-08-01 * Opening | balance
    assets:bank:checking   1000 USD
    equity:opening-balances

2026-08-20 * Corner Shop | groceries
    expenses:food            25 USD
    assets:bank:checking
"""

DIRTY_JOURNAL = """\
account assets:bank:checking
    type:A
account expenses:food
account expenses:uncategorized
account liabilities:credit-card:citi
    type:L
account equity:opening-balances
    type:E

2026-01-05 * Opening | balance
    assets:bank:checking     1000 USD
    equity:opening-balances

2026-02-01 * EDEKA | groceries
    expenses:uncategorized     40 USD
    equity:opening-balances

2026-03-10 * Citi | a real charge months before the correction
    liabilities:credit-card:citi    -80 USD
    expenses:food

2026-08-26 * Citi balance correction per OFX 2026-08-26
    liabilities:credit-card:citi   -1766.23 USD
    equity:opening-balances
"""


@unittest.skipUnless(HLEDGER, "hledger not on PATH")
class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write_journal(self, text):
        p = Path(self._tmp.name) / "main.journal"
        p.write_text(text, encoding="utf-8")
        return str(p)

    def run_cli(self, journal):
        return subprocess.run(
            ["python3", os.path.join(os.path.dirname(__file__), "audit_ledger.py"),
             "--journal", journal, "--today", "2026-09-01"],
            capture_output=True, text=True,
        )

    def test_should_produce_no_high_or_warn_findings_for_a_clean_journal(self):
        r = self.run_cli(self.write_journal(CLEAN_JOURNAL))
        self.assertEqual(r.returncode, 0, r.stderr)
        body_checks = [ln.split("\t")[1] for ln in r.stdout.splitlines()
                       if ln[:1] in ("h", "w") and "\t" in ln and not ln.startswith("#")]
        self.assertNotIn("wrong-sign", body_checks)
        self.assertNotIn("equity-plug", body_checks)

    def test_should_flag_the_equity_plug_and_the_lump_correction_in_a_dirty_journal(self):
        r = self.run_cli(self.write_journal(DIRTY_JOURNAL))
        self.assertEqual(r.returncode, 0, r.stderr)
        checks = {ln.split("\t")[1] for ln in r.stdout.splitlines()
                  if "\t" in ln and not ln.startswith("#")}
        self.assertIn("equity-plug", checks)          # the EDEKA posting
        self.assertIn("late-lump-adjustment", checks)  # the Citi correction
        self.assertIn("missing-type", checks)          # expenses:food has no type:

    def test_should_exit_2_when_the_journal_is_missing(self):
        r = self.run_cli(os.path.join(self._tmp.name, "nope.journal"))
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
