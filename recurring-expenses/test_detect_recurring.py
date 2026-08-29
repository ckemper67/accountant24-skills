#!/usr/bin/env python3
"""Tests for detect_recurring.py.

Run with: python3 -m unittest test_detect_recurring -v   (from this directory)

Standard library only (unittest), matching the script - the vendored
interpreter has no pip. Expected values are hand-computed from each fixture,
not re-derived by calling the script's own formulas, so a regression in the
arithmetic fails a test instead of being rubber-stamped by it.
"""
import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

import detect_recurring as dr


def monthly_dates(start, n):
    """n dates one calendar month apart, starting at `start` (a date)."""
    out = []
    y, m, d = start.year, start.month, start.day
    for _ in range(n):
        out.append(date(y, m, d))
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


# ---- parse_amounts --------------------------------------------------------


class TestParseAmounts(unittest.TestCase):
    def test_should_parse_a_suffix_commodity(self):
        self.assertEqual(list(dr.parse_amounts("12.99 EUR")), [("EUR", 12.99)])

    def test_should_parse_a_dollar_prefix(self):
        self.assertEqual(list(dr.parse_amounts("$12.99")), [("$", 12.99)])

    def test_should_parse_a_prefixed_letter_commodity(self):
        self.assertEqual(list(dr.parse_amounts("USD 12.99")), [("USD", 12.99)])

    def test_should_default_commodity_to_question_mark_for_a_bare_number(self):
        self.assertEqual(list(dr.parse_amounts("12.99")), [("?", 12.99)])

    def test_should_strip_thousands_separators(self):
        self.assertEqual(list(dr.parse_amounts("1,234.56 USD")), [("USD", 1234.56)])

    def test_should_keep_a_negative_sign(self):
        self.assertEqual(list(dr.parse_amounts("-5.00 EUR")), [("EUR", -5.0)])

    def test_should_yield_nothing_for_an_empty_cell(self):
        self.assertEqual(list(dr.parse_amounts("   ")), [])

    def test_should_yield_each_amount_in_a_multi_commodity_cell(self):
        self.assertEqual(
            list(dr.parse_amounts("10.00 EUR, -5.00 USD")),
            [("EUR", 10.0), ("USD", -5.0)],
        )


# ---- payee_of / norm_payee ----------------------------------------------


class TestPayee(unittest.TestCase):
    def test_should_take_description_text_before_the_first_pipe(self):
        self.assertEqual(dr.payee_of("Netflix | monthly plan"), "Netflix")

    def test_should_return_the_whole_description_when_there_is_no_pipe(self):
        self.assertEqual(dr.payee_of("Corner Shop"), "Corner Shop")

    def test_should_fold_case_when_normalizing(self):
        self.assertEqual(dr.norm_payee("NETFLIX"), dr.norm_payee("Netflix"))

    def test_should_collapse_internal_whitespace_when_normalizing(self):
        self.assertEqual(dr.norm_payee("Corner   Shop"), "corner shop")

    def test_should_not_merge_names_that_differ_by_more_than_case(self):
        self.assertNotEqual(dr.norm_payee("NETFLIX.COM"), dr.norm_payee("Netflix"))


# ---- parse_date --------------------------------------------------------


class TestParseDate(unittest.TestCase):
    def test_should_parse_an_iso_date(self):
        self.assertEqual(dr.parse_date("2026-02-11"), date(2026, 2, 11))

    def test_should_parse_a_slash_date(self):
        self.assertEqual(dr.parse_date("2026/02/11"), date(2026, 2, 11))

    def test_should_raise_on_an_unparseable_date(self):
        with self.assertRaises(ValueError):
            dr.parse_date("11 Feb 2026")


# ---- guess_cadence ----------------------------------------------------


class TestGuessCadence(unittest.TestCase):
    def test_should_call_a_30_day_gap_monthly(self):
        label, dpp, reg = dr.guess_cadence([30, 30, 30])
        self.assertEqual(label, "monthly")
        self.assertEqual(dpp, 30.44)
        self.assertEqual(reg, 1.0)

    def test_should_call_a_7_day_gap_weekly(self):
        self.assertEqual(dr.guess_cadence([7, 7, 7, 7])[0], "weekly")

    def test_should_call_a_365_day_gap_yearly(self):
        label, dpp, reg = dr.guess_cadence([365])
        self.assertEqual(label, "yearly")
        self.assertEqual(dpp, 365.25)
        self.assertEqual(reg, 1.0)

    def test_should_call_a_91_day_gap_quarterly(self):
        self.assertEqual(dr.guess_cadence([90, 91, 92])[0], "quarterly")

    def test_should_return_single_for_no_intervals(self):
        self.assertEqual(dr.guess_cadence([]), ("single", 0.0, 0.0))

    def test_should_score_regularity_as_the_fraction_of_gaps_that_fit(self):
        # median([30, 30, 200]) = 30 -> monthly bucket 24..38; 200 is outside.
        label, _dpp, reg = dr.guess_cadence([30, 30, 200])
        self.assertEqual(label, "monthly")
        self.assertEqual(reg, 0.67)

    def test_should_return_irregular_when_the_median_gap_fits_no_bucket(self):
        # median([50, 200, 55]) = 55 -> no bucket; band 27.5..82.5 holds 50 and 55.
        label, _dpp, reg = dr.guess_cadence([50, 200, 55])
        self.assertEqual(label, "irregular")
        self.assertEqual(reg, 0.67)


# ---- classify_amounts -----------------------------------------------


def dated(pairs):
    return [(date.fromisoformat(d), a) for d, a in pairs]


class TestClassifyAmounts(unittest.TestCase):
    def test_should_call_a_constant_series_identical(self):
        shape, detail, rep = dr.classify_amounts(
            dated([("2025-01-01", 10.0), ("2025-02-01", 10.0), ("2025-03-01", 10.0)])
        )
        self.assertEqual((shape, detail, rep), ("identical", "10.00", 10.0))

    def test_should_call_a_single_rising_change_a_step_and_report_its_date(self):
        shape, detail, rep = dr.classify_amounts(
            dated([
                ("2025-01-10", 9.99), ("2025-02-10", 9.99),
                ("2025-03-10", 11.99), ("2025-04-10", 11.99),
            ])
        )
        self.assertEqual(shape, "step")
        self.assertEqual(detail, "9.99->11.99@2025-03-10")
        self.assertEqual(rep, 11.99)

    def test_should_call_a_falling_change_a_step_too(self):
        shape, _detail, rep = dr.classify_amounts(
            dated([("2025-01-01", 20.0), ("2025-02-01", 20.0), ("2025-03-01", 15.0), ("2025-04-01", 15.0)])
        )
        self.assertEqual(shape, "step")
        self.assertEqual(rep, 15.0)

    def test_should_call_a_stable_band_banded_and_report_the_range(self):
        # values 90,110,130,100,120 -> 4 transitions, ratio 130/90 = 1.44 <= 4.
        shape, detail, rep = dr.classify_amounts(
            dated([
                ("2025-01-01", 90.0), ("2025-02-01", 110.0), ("2025-03-01", 130.0),
                ("2025-04-01", 100.0), ("2025-05-01", 120.0),
            ])
        )
        self.assertEqual(shape, "banded")
        self.assertEqual(detail, "90.00-130.00")
        self.assertEqual(rep, 110.0)  # median of [90,100,110,120,130]

    def test_should_call_a_wide_bouncing_series_irregular(self):
        # values 10,100,10,100 -> 3 transitions, not monotonic, ratio 10 > 4.
        shape, detail, rep = dr.classify_amounts(
            dated([("2025-01-01", 10.0), ("2025-02-01", 100.0), ("2025-03-01", 10.0), ("2025-04-01", 100.0)])
        )
        self.assertEqual(shape, "irregular")
        self.assertEqual(detail, "10.00-100.00")
        self.assertEqual(rep, 55.0)


# ---- analyze --------------------------------------------------------


def group(payee, commodity, dates, amounts, accounts=None):
    accounts = accounts or ["Expenses:Subscriptions"] * len(dates)
    return [
        (payee, commodity, d, a, acct)
        for d, a, acct in zip(dates, amounts, accounts)
    ]


class TestAnalyze(unittest.TestCase):
    def test_should_report_a_clean_monthly_series_as_a_yes_candidate(self):
        dates = monthly_dates(date(2025, 1, 5), 5)
        fact = dr.analyze(group("Netflix", "EUR", dates, [10.0] * 5), today=date(2025, 6, 1))
        self.assertEqual(fact["recurring"], "yes")
        self.assertEqual(fact["cadence"], "monthly")
        self.assertEqual(fact["months"], 5)
        self.assertEqual(fact["shape"], "identical")
        self.assertEqual(fact["approx_monthly"], "10.00")
        self.assertEqual(fact["status"], "active")
        self.assertEqual(fact["first"], "2025-01-05")
        self.assertEqual(fact["last"], "2025-05-05")

    def test_should_drop_a_series_seen_in_fewer_than_three_months_that_is_not_annual(self):
        fact = dr.analyze(
            group("Corner Shop", "EUR", [date(2025, 1, 1), date(2025, 2, 1)], [5.0, 5.0]),
            today=date(2025, 3, 1),
        )
        self.assertIsNone(fact)

    def test_should_keep_a_two_charge_series_about_a_year_apart(self):
        fact = dr.analyze(
            group("Annual Domain", "USD", [date(2024, 5, 1), date(2025, 5, 1)], [120.0, 120.0]),
            today=date(2025, 6, 1),
        )
        self.assertIsNotNone(fact)
        self.assertEqual(fact["cadence"], "yearly")
        self.assertEqual(fact["months"], 2)
        # 120 * (30.44 / 365.25) = 10.00
        self.assertEqual(fact["approx_monthly"], "10.00")

    def test_should_flag_a_series_gone_quiet_as_overdue(self):
        dates = monthly_dates(date(2024, 10, 5), 4)  # ends 2025-01-05
        fact = dr.analyze(group("Old Gym", "EUR", dates, [25.0] * 4), today=date(2025, 5, 1))
        # 116 days since last charge / 30.44 = 3.8 cadences
        self.assertEqual(fact["status"], "overdue~3.8x")

    def test_should_normalize_a_quarterly_amount_to_a_monthly_figure(self):
        dates = [date(2024, 1, 15), date(2024, 4, 15), date(2024, 7, 15), date(2024, 10, 15)]
        fact = dr.analyze(group("Water Co", "EUR", dates, [90.0] * 4), today=date(2024, 11, 1))
        self.assertEqual(fact["cadence"], "quarterly")
        # 90 * (30.44 / 91.31) = 30.00
        self.assertEqual(fact["approx_monthly"], "30.00")

    def test_should_aggregate_the_accounts_a_payee_posted_to(self):
        dates = monthly_dates(date(2025, 1, 1), 3)
        accts = ["Expenses:Software", "Expenses:Software", "Expenses:Subscriptions"]
        fact = dr.analyze(group("Acme", "EUR", dates, [8.0] * 3, accts), today=date(2025, 4, 1))
        self.assertEqual(fact["accounts"], "Expenses:Software (2), Expenses:Subscriptions (1)")

    def test_should_call_an_irregular_frequent_payee_not_recurring(self):
        # groceries: bought often, no schedule
        dates = [date(2025, 1, 3), date(2025, 1, 19), date(2025, 2, 2), date(2025, 2, 27), date(2025, 4, 8)]
        amounts = [40.0, 12.0, 85.0, 30.0, 60.0]
        fact = dr.analyze(group("Supermarket", "EUR", dates, amounts), today=date(2025, 4, 20))
        self.assertEqual(fact["recurring"], "no")


# ---- build_report -------------------------------------------------


class TestBuildReport(unittest.TestCase):
    def test_should_group_case_variants_together_but_keep_distinct_names_apart(self):
        jan = monthly_dates(date(2025, 1, 1), 3)
        postings = []
        for d in jan:
            postings.append(("NETFLIX", "netflix", "EUR", d, 10.0, "Expenses:Subscriptions"))
        for d in jan:
            postings.append(("Netflix", "netflix", "EUR", d, 10.0, "Expenses:Subscriptions"))
        for d in jan:
            postings.append(("NETFLIX.COM", "netflix.com", "EUR", d, 3.0, "Expenses:Subscriptions"))
        rows, skipped = dr.build_report(postings, today=date(2025, 4, 1))
        self.assertEqual(len(rows), 2)
        self.assertEqual(skipped, 0)
        by_name = {r["payee"] for r in rows}
        self.assertEqual(by_name, {"NETFLIX", "NETFLIX.COM"})
        merged = next(r for r in rows if r["payee"] == "NETFLIX")
        self.assertEqual(merged["postings"], 6)

    def test_should_sort_yes_candidates_before_no_and_by_monthly_cost_descending(self):
        jan = monthly_dates(date(2025, 1, 1), 4)
        postings = []
        for d in jan:  # clean 50/mo -> yes
            postings.append(("Rent Bump", "rent bump", "EUR", d, 50.0, "Expenses:Rent"))
        for d in jan:  # clean 5/mo -> yes
            postings.append(("Tiny Sub", "tiny sub", "EUR", d, 5.0, "Expenses:Subscriptions"))
        noisy = [date(2025, 1, 2), date(2025, 1, 20), date(2025, 3, 4), date(2025, 4, 30)]
        for d, a in zip(noisy, [90.0, 10.0, 200.0, 30.0]):  # irregular -> no
            postings.append(("Random", "random", "EUR", d, a, "Expenses:Misc"))
        rows, _skipped = dr.build_report(postings, today=date(2025, 5, 15))
        self.assertEqual([r["payee"] for r in rows], ["Rent Bump", "Tiny Sub", "Random"])

    def test_should_count_groups_dropped_for_too_little_history(self):
        postings = [
            ("One Off", "one off", "EUR", date(2025, 1, 1), 9.0, "Expenses:Misc"),
            ("One Off", "one off", "EUR", date(2025, 2, 1), 9.0, "Expenses:Misc"),
        ]
        rows, skipped = dr.build_report(postings, today=date(2025, 3, 1))
        self.assertEqual(rows, [])
        self.assertEqual(skipped, 1)


# ---- parse_args --------------------------------------------------


class TestParseArgs(unittest.TestCase):
    def test_should_accept_a_lone_csv_path(self):
        path, today = dr.parse_args(["ledger.csv"])
        self.assertEqual(path, "ledger.csv")
        self.assertEqual(today, date.today())

    def test_should_accept_today_as_a_separate_argument(self):
        self.assertEqual(dr.parse_args(["x.csv", "--today", "2025-01-02"]), ("x.csv", date(2025, 1, 2)))

    def test_should_accept_today_with_an_equals_sign(self):
        self.assertEqual(dr.parse_args(["x.csv", "--today=2025-01-02"]), ("x.csv", date(2025, 1, 2)))

    def test_should_accept_options_before_the_path(self):
        self.assertEqual(dr.parse_args(["--today", "2025-01-02", "x.csv"]), ("x.csv", date(2025, 1, 2)))

    def test_should_reject_today_with_no_value(self):
        with self.assertRaises(SystemExit):
            dr.parse_args(["x.csv", "--today"])

    def test_should_reject_an_unknown_option(self):
        with self.assertRaises(SystemExit):
            dr.parse_args(["x.csv", "--verbose"])

    def test_should_reject_a_missing_path(self):
        with self.assertRaises(SystemExit):
            dr.parse_args(["--today", "2025-01-02"])

    def test_should_reject_more_than_one_path(self):
        with self.assertRaises(SystemExit):
            dr.parse_args(["a.csv", "b.csv"])


# ---- read_postings + main (end to end) -------------------------


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write_csv(self, rows, name="reg.csv"):
        path = Path(self._tmp.name) / name
        header = ["txnidx", "date", "code", "description", "account", "amount", "total"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return str(path)


class TestReadPostings(TempDirTestCase):
    def test_should_skip_zero_and_negative_amounts(self):
        path = self.write_csv([
            ["1", "2025-01-01", "", "Netflix", "Expenses:Subscriptions", "10.00 EUR", ""],
            ["2", "2025-02-01", "", "Netflix", "Expenses:Subscriptions", "0", ""],
            ["3", "2025-03-01", "", "Netflix", "Expenses:Subscriptions", "-4.00 EUR", ""],
        ])
        rows = dr.read_postings(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], 10.0)

    def test_should_split_the_payee_on_a_pipe(self):
        path = self.write_csv([
            ["1", "2025-01-01", "", "Spotify | Premium", "Expenses:Subscriptions", "11.99 EUR", ""],
        ])
        self.assertEqual(dr.read_postings(path)[0][0], "Spotify")

    def test_should_skip_rows_with_an_unparseable_date(self):
        path = self.write_csv([
            ["1", "not-a-date", "", "Netflix", "Expenses:Subscriptions", "10.00 EUR", ""],
        ])
        self.assertEqual(dr.read_postings(path), [])

    def test_should_raise_when_a_required_column_is_missing(self):
        path = Path(self._tmp.name) / "bad.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["date", "description", "account"], ["2025-01-01", "X", "Expenses:Misc"]])
        with self.assertRaises(SystemExit):
            dr.read_postings(str(path))


class TestMain(TempDirTestCase):
    def test_should_print_a_header_and_one_row_per_candidate(self):
        rows = []
        for i, d in enumerate(monthly_dates(date(2025, 1, 8), 6), start=1):
            rows.append([str(i), d.isoformat(), "", "Netflix", "Expenses:Subscriptions", "12.99 EUR", ""])
        # a second, irregular payee that should be dropped
        for i, d in enumerate([date(2025, 1, 2), date(2025, 3, 9)], start=100):
            rows.append([str(i), d.isoformat(), "", "Corner Shop", "Expenses:Misc", "7.00 EUR", ""])
        path = self.write_csv(rows)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = dr.main([path, "--today", "2025-07-15"])
        self.assertEqual(rc, 0)

        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "# " + "\t".join(dr.COLUMNS))
        data = [ln for ln in lines if not ln.startswith("#")]
        self.assertEqual(len(data), 1)
        fields = dict(zip(dr.COLUMNS, data[0].split("\t")))
        self.assertEqual(fields["payee"], "Netflix")
        self.assertEqual(fields["recurring"], "yes")
        self.assertEqual(fields["cadence"], "monthly")
        self.assertEqual(fields["approx_monthly"], "12.99")
        self.assertEqual(lines[-1], "# skipped 1 payee/commodity group(s) with fewer than 3 charge months and no annual pair")


if __name__ == "__main__":
    unittest.main()
