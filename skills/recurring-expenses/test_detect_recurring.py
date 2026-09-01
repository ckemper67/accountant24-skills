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
from datetime import date, timedelta
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


def dated(pairs):
    """[(date, amount)] from [("YYYY-MM-DD", amount)]."""
    return [(date.fromisoformat(d), a) for d, a in pairs]


def series(payee, commodity, start_iso, gaps, amount, account="Expenses:Subscriptions"):
    """A payee+commodity group (payee, commodity, date, amount, account) tuples,
    with charge dates start, start+gaps[0], start+gaps[0]+gaps[1], ..."""
    d = date.fromisoformat(start_iso)
    dates = [d]
    for g in gaps:
        d = d + timedelta(days=g)
        dates.append(d)
    amts = amount if isinstance(amount, list) else [amount] * len(dates)
    return [(payee, commodity, dd, a, account) for dd, a in zip(dates, amts)]


# ---- parse_number -------------------------------------------------------


class TestParseNumber(unittest.TestCase):
    def test_should_parse_a_plain_decimal(self):
        self.assertEqual(dr.parse_number("12.99"), 12.99)

    def test_should_treat_a_lone_comma_before_two_digits_as_the_decimal_mark(self):
        self.assertEqual(dr.parse_number("12,99"), 12.99)

    def test_should_treat_a_lone_comma_before_three_digits_as_a_thousands_separator(self):
        self.assertEqual(dr.parse_number("1,234"), 1234.0)

    def test_should_treat_a_lone_dot_before_three_digits_as_a_thousands_separator(self):
        self.assertEqual(dr.parse_number("1.234"), 1234.0)

    def test_should_parse_us_grouped_notation(self):
        self.assertEqual(dr.parse_number("1,234.56"), 1234.56)

    def test_should_parse_european_grouped_notation(self):
        self.assertEqual(dr.parse_number("1.234,56"), 1234.56)

    def test_should_parse_european_notation_with_several_groups(self):
        self.assertEqual(dr.parse_number("1.234.567,89"), 1234567.89)

    def test_should_strip_multiple_thousands_separators(self):
        self.assertEqual(dr.parse_number("12,345,678"), 12345678.0)

    def test_should_keep_the_sign(self):
        self.assertEqual(dr.parse_number("-5.00"), -5.0)

    def test_should_return_none_when_there_is_no_digit(self):
        self.assertIsNone(dr.parse_number(""))
        self.assertIsNone(dr.parse_number("  -  "))

    def test_should_document_the_three_decimal_place_limitation(self):
        # "12.999 KWD" (3-dp commodity, no thousands sep) is read as 12999 -
        # a known, documented limitation. Pinned so the behavior is a choice.
        self.assertEqual(dr.parse_number("12.999"), 12999.0)


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

    def test_should_parse_a_us_grouped_amount(self):
        self.assertEqual(list(dr.parse_amounts("1,234.56 USD")), [("USD", 1234.56)])

    def test_should_parse_a_european_grouped_amount(self):
        self.assertEqual(list(dr.parse_amounts("1.234,56 EUR")), [("EUR", 1234.56)])

    def test_should_keep_a_negative_sign(self):
        self.assertEqual(list(dr.parse_amounts("-5.00 EUR")), [("EUR", -5.0)])

    def test_should_yield_nothing_for_an_empty_cell(self):
        self.assertEqual(list(dr.parse_amounts("   ")), [])

    def test_should_yield_each_amount_in_a_multi_commodity_cell(self):
        self.assertEqual(
            list(dr.parse_amounts("10.00 EUR, -5.00 USD")),
            [("EUR", 10.0), ("USD", -5.0)],
        )

    def test_should_drop_a_cost_annotation_after_an_at_sign(self):
        # "100.00 USD @ 0.92 EUR" - the 0.92 rate must not become a second charge
        self.assertEqual(list(dr.parse_amounts("100.00 USD @ 0.92 EUR")), [("USD", 100.0)])


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

    def test_should_call_a_183_day_gap_semiannual(self):
        label, dpp, _reg = dr.guess_cadence([182, 184])
        self.assertEqual(label, "semiannual")
        self.assertEqual(dpp, 182.62)

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

    def test_should_include_the_upper_bound_of_a_bucket(self):
        # weekly is 5..10 inclusive; 10 is weekly, 11 is biweekly.
        self.assertEqual(dr.guess_cadence([10, 10])[0], "weekly")
        self.assertEqual(dr.guess_cadence([11, 11])[0], "biweekly")

    def test_should_fall_into_the_dead_zone_just_past_monthly(self):
        # 38 is the top of monthly; 39 fits no bucket.
        self.assertEqual(dr.guess_cadence([38, 38])[0], "monthly")
        label, dpp, _reg = dr.guess_cadence([39, 39])
        self.assertEqual(label, "irregular")
        self.assertEqual(dpp, 39.0)  # irregular dpp is the median gap


# ---- classify_amounts -----------------------------------------------


class TestClassifyAmounts(unittest.TestCase):
    def test_should_call_a_constant_series_identical(self):
        shape, detail, rep = dr.classify_amounts(
            dated([("2025-01-01", 10.0), ("2025-02-01", 10.0), ("2025-03-01", 10.0)])
        )
        self.assertEqual((shape, detail, rep), ("identical", "10.00", 10.0))

    def test_should_handle_exactly_two_identical_values(self):
        self.assertEqual(
            dr.classify_amounts(dated([("2025-01-01", 10.0), ("2025-02-01", 10.0)])),
            ("identical", "10.00", 10.0),
        )

    def test_should_handle_exactly_two_differing_values_as_a_step(self):
        shape, detail, rep = dr.classify_amounts(
            dated([("2025-01-01", 10.0), ("2025-02-01", 12.0)])
        )
        self.assertEqual((shape, detail, rep), ("step", "10.00->12.00@2025-02-01", 12.0))

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

    def test_should_allow_up_to_three_transitions_in_a_step(self):
        shape, detail, _rep = dr.classify_amounts(
            dated([("2025-01-01", 10.0), ("2025-02-01", 11.0), ("2025-03-01", 12.0), ("2025-04-01", 13.0)])
        )
        self.assertEqual(shape, "step")
        self.assertEqual(detail, "10.00->11.00@2025-02-01->12.00@2025-03-01->13.00@2025-04-01")

    def test_should_stop_calling_it_a_step_at_the_fourth_transition(self):
        shape, _detail, _rep = dr.classify_amounts(
            dated([
                ("2025-01-01", 10.0), ("2025-02-01", 11.0), ("2025-03-01", 12.0),
                ("2025-04-01", 13.0), ("2025-05-01", 14.0),
            ])
        )
        self.assertEqual(shape, "banded")  # 4 transitions -> no longer a step

    def test_should_not_treat_a_sub_half_percent_wiggle_as_a_step(self):
        # 100.00 -> 100.40 is 0.4%, under the 0.5%-or-1-cent threshold.
        shape, _detail, _rep = dr.classify_amounts(
            dated([("2025-01-01", 100.0), ("2025-02-01", 100.4), ("2025-03-01", 100.0)])
        )
        self.assertEqual(shape, "banded")

    def test_should_treat_an_above_threshold_move_as_a_step(self):
        shape, _detail, _rep = dr.classify_amounts(
            dated([("2025-01-01", 100.0), ("2025-02-01", 100.6), ("2025-03-01", 100.6)])
        )
        self.assertEqual(shape, "step")

    def test_should_call_a_stable_band_banded_and_report_the_range(self):
        shape, detail, rep = dr.classify_amounts(
            dated([
                ("2025-01-01", 90.0), ("2025-02-01", 110.0), ("2025-03-01", 130.0),
                ("2025-04-01", 100.0), ("2025-05-01", 120.0),
            ])
        )
        self.assertEqual(shape, "banded")
        self.assertEqual(detail, "90.00-130.00")
        self.assertEqual(rep, 110.0)  # median of [90,100,110,120,130]

    def test_should_include_a_four_times_ratio_in_banded(self):
        # 40/10 == 4.0 exactly -> banded; 41/10 == 4.1 -> irregular
        banded, detail, _rep = dr.classify_amounts(
            dated([("2025-01-01", 10.0), ("2025-02-01", 40.0), ("2025-03-01", 10.0),
                   ("2025-04-01", 40.0), ("2025-05-01", 10.0)])
        )
        self.assertEqual((banded, detail), ("banded", "10.00-40.00"))
        wide, _d, _r = dr.classify_amounts(
            dated([("2025-01-01", 10.0), ("2025-02-01", 41.0), ("2025-03-01", 10.0),
                   ("2025-04-01", 41.0), ("2025-05-01", 10.0)])
        )
        self.assertEqual(wide, "irregular")


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

    def test_should_drop_a_two_charge_series_whose_gap_matches_no_cadence(self):
        # 50 days apart -> median gap 50 -> fits no bucket -> irregular -> dropped
        fact = dr.analyze(
            group("Corner Shop", "EUR", [date(2025, 1, 1), date(2025, 2, 20)], [5.0, 5.0]),
            today=date(2025, 3, 1),
        )
        self.assertIsNone(fact)

    def test_should_keep_a_two_month_old_monthly_series_as_weak(self):
        # only ~6 weeks of history, one 31-day gap -> plausible monthly bill
        fact = dr.analyze(
            group("New Rent", "EUR", [date(2025, 1, 1), date(2025, 2, 1)], [900.0, 900.0]),
            today=date(2025, 2, 10),
        )
        self.assertIsNotNone(fact)
        self.assertEqual(fact["cadence"], "monthly")
        self.assertEqual(fact["months"], 2)
        self.assertEqual(fact["recurring"], "weak")  # one interval is never "yes"

    def test_should_keep_a_two_charge_series_about_a_year_apart_but_only_as_weak(self):
        fact = dr.analyze(
            group("Annual Domain", "USD", [date(2024, 5, 1), date(2025, 5, 1)], [120.0, 120.0]),
            today=date(2025, 6, 1),
        )
        self.assertIsNotNone(fact)
        self.assertEqual(fact["cadence"], "yearly")
        self.assertEqual(fact["months"], 2)
        self.assertEqual(fact["postings"], 2)
        # one interval is never enough evidence for "yes"
        self.assertEqual(fact["recurring"], "weak")
        # 120 * (30.44 / 365.25) = 10.00
        self.assertEqual(fact["approx_monthly"], "10.00")

    def test_should_call_a_three_charge_annual_series_yes(self):
        fact = dr.analyze(
            group("Insurance", "EUR", [date(2023, 5, 1), date(2024, 5, 1), date(2025, 5, 1)], [120.0] * 3),
            today=date(2025, 6, 1),
        )
        self.assertEqual(fact["cadence"], "yearly")
        self.assertEqual(fact["recurring"], "yes")
        self.assertEqual(fact["approx_monthly"], "10.00")

    def test_should_report_a_semiannual_series(self):
        fact = dr.analyze(series("Water", "EUR", "2024-01-01", [182, 184], 300.0), today=date(2025, 2, 1))
        self.assertEqual(fact["cadence"], "semiannual")
        self.assertEqual(fact["recurring"], "yes")
        # 300 * (30.44 / 182.62) = 50.0084 -> 50.01
        self.assertEqual(fact["approx_monthly"], "50.01")

    def test_should_classify_exactly_at_the_no_boundary_as_weak_not_no(self):
        # gaps [30,30,5,200] -> median 30 (monthly), 2 of 4 fit -> regularity 0.50
        fact = dr.analyze(series("Half", "EUR", "2025-01-01", [30, 30, 5, 200], 10.0), today=date(2025, 10, 1))
        self.assertEqual(fact["regularity"], "0.50")
        self.assertEqual(fact["recurring"], "weak")  # regularity < 0.5 is strict

    def test_should_classify_exactly_at_the_yes_boundary_as_yes(self):
        # gaps [30,30,30,200] -> median 30 (monthly), 3 of 4 fit -> regularity 0.75
        fact = dr.analyze(series("Three", "EUR", "2025-01-01", [30, 30, 30, 200], 10.0), today=date(2025, 11, 1))
        self.assertEqual(fact["regularity"], "0.75")
        self.assertEqual(fact["recurring"], "yes")  # regularity >= 0.75

    def test_should_stay_active_one_day_short_of_the_overdue_threshold(self):
        # monthly dpp 30.44 -> overdue after 45.66 days; test 45 vs 46
        g = series("Gym", "EUR", "2024-10-15", [31, 30, 31], 25.0)  # last charge 2025-01-15
        self.assertEqual(dr.analyze(g, today=date(2025, 3, 1))["status"], "active")   # 45 days
        self.assertEqual(dr.analyze(g, today=date(2025, 3, 2))["status"], "overdue~1.5x")  # 46 days

    def test_should_flag_a_series_gone_quiet_as_overdue(self):
        dates = monthly_dates(date(2024, 10, 5), 4)  # ends 2025-01-05
        fact = dr.analyze(group("Old Gym", "EUR", dates, [25.0] * 4), today=date(2025, 5, 1))
        # 116 days since last charge / 30.44 = 3.8 cadences
        self.assertEqual(fact["status"], "overdue~3.8x")

    def test_should_stay_active_when_today_is_before_the_last_charge(self):
        g = series("Future", "EUR", "2025-03-05", [31, 30, 31], 9.0)  # last 2025-06-05
        self.assertEqual(dr.analyze(g, today=date(2025, 3, 1))["status"], "active")

    def test_should_normalize_a_quarterly_amount_to_a_monthly_figure(self):
        dates = [date(2024, 1, 15), date(2024, 4, 15), date(2024, 7, 15), date(2024, 10, 15)]
        fact = dr.analyze(group("Water Co", "EUR", dates, [90.0] * 4), today=date(2024, 11, 1))
        self.assertEqual(fact["cadence"], "quarterly")
        # 90 * (30.44 / 91.31) = 30.00
        self.assertEqual(fact["approx_monthly"], "30.00")

    def test_should_collapse_same_day_duplicate_charges_for_cadence_but_not_the_count(self):
        dates = [date(2025, 1, 5), date(2025, 1, 5), date(2025, 2, 5), date(2025, 3, 5), date(2025, 4, 5)]
        fact = dr.analyze(group("Acme", "EUR", dates, [10.0] * 5), today=date(2025, 5, 1))
        self.assertEqual(fact["postings"], 5)
        self.assertEqual(fact["months"], 4)
        self.assertEqual(fact["cadence"], "monthly")

    def test_should_aggregate_the_accounts_a_payee_posted_to(self):
        dates = monthly_dates(date(2025, 1, 1), 3)
        accts = ["Expenses:Software", "Expenses:Software", "Expenses:Subscriptions"]
        fact = dr.analyze(group("Acme", "EUR", dates, [8.0] * 3, accts), today=date(2025, 4, 1))
        self.assertEqual(fact["accounts"], "Expenses:Software (2), Expenses:Subscriptions (1)")

    def test_should_call_an_irregular_frequent_payee_not_recurring(self):
        dates = [date(2025, 1, 3), date(2025, 1, 19), date(2025, 2, 2), date(2025, 2, 27), date(2025, 4, 8)]
        amounts = [40.0, 12.0, 85.0, 30.0, 60.0]
        fact = dr.analyze(group("Supermarket", "EUR", dates, amounts), today=date(2025, 4, 20))
        self.assertEqual(fact["recurring"], "no")


# ---- build_report -------------------------------------------------


class TestBuildReport(unittest.TestCase):
    def test_should_emit_one_row_per_commodity_for_a_payee_billed_in_two(self):
        postings = []
        for d in monthly_dates(date(2025, 1, 1), 3):
            postings.append(("Acme", "acme", "EUR", d, 10.0, "Expenses:Software"))
            postings.append(("Acme", "acme", "USD", d, 12.0, "Expenses:Software"))
        rows, skipped = dr.build_report(postings, today=date(2025, 4, 1))
        self.assertEqual(skipped, 0)
        self.assertEqual({r["commodity"] for r in rows}, {"EUR", "USD"})
        self.assertTrue(all(r["postings"] == 3 for r in rows))

    def test_should_break_ties_by_payee_so_ordering_is_stable(self):
        postings = []
        for d in monthly_dates(date(2025, 1, 1), 4):
            postings.append(("Zebra", "zebra", "EUR", d, 10.0, "Expenses:X"))
            postings.append(("Alpha", "alpha", "EUR", d, 10.0, "Expenses:X"))
        rows, _skipped = dr.build_report(postings, today=date(2025, 5, 15))
        self.assertEqual([r["payee"] for r in rows], ["Alpha", "Zebra"])

    def test_should_sort_yes_candidates_before_no_and_by_monthly_cost_descending(self):
        jan = monthly_dates(date(2025, 1, 1), 4)
        postings = []
        for d in jan:
            postings.append(("Rent Bump", "rent bump", "EUR", d, 50.0, "Expenses:Rent"))
        for d in jan:
            postings.append(("Tiny Sub", "tiny sub", "EUR", d, 5.0, "Expenses:Subscriptions"))
        noisy = [date(2025, 1, 2), date(2025, 1, 20), date(2025, 3, 4), date(2025, 4, 30)]
        for d, a in zip(noisy, [90.0, 10.0, 200.0, 30.0]):
            postings.append(("Random", "random", "EUR", d, a, "Expenses:Misc"))
        rows, _skipped = dr.build_report(postings, today=date(2025, 5, 15))
        self.assertEqual([r["payee"] for r in rows], ["Rent Bump", "Tiny Sub", "Random"])

    def test_should_count_groups_dropped_for_too_little_history(self):
        # two charges 50 days apart -> no cadence -> not a candidate
        postings = [
            ("One Off", "one off", "EUR", date(2025, 1, 1), 9.0, "Expenses:Misc"),
            ("One Off", "one off", "EUR", date(2025, 2, 20), 9.0, "Expenses:Misc"),
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

    def write_csv(self, rows, name="reg.csv", header=None):
        path = Path(self._tmp.name) / name
        header = header or ["txnidx", "date", "code", "description", "account", "amount", "total"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return str(path)


class TestReadPostings(TempDirTestCase):
    def test_should_skip_zero_and_negative_amounts_and_count_them(self):
        path = self.write_csv([
            ["1", "2025-01-01", "", "Netflix", "Expenses:Subscriptions", "10.00 EUR", ""],
            ["2", "2025-02-01", "", "Netflix", "Expenses:Subscriptions", "0", ""],
            ["3", "2025-03-01", "", "Netflix", "Expenses:Subscriptions", "-4.00 EUR", ""],
        ])
        postings, drops = dr.read_postings(path)
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0][4], 10.0)
        self.assertEqual(drops["no_positive_amount"], 2)

    def test_should_split_the_payee_on_a_pipe(self):
        path = self.write_csv([
            ["1", "2025-01-01", "", "Spotify | Premium", "Expenses:Subscriptions", "11.99 EUR", ""],
        ])
        postings, _drops = dr.read_postings(path)
        self.assertEqual(postings[0][0], "Spotify")

    def test_should_skip_rows_with_an_unparseable_date_and_count_them(self):
        path = self.write_csv([
            ["1", "not-a-date", "", "Netflix", "Expenses:Subscriptions", "10.00 EUR", ""],
        ])
        postings, drops = dr.read_postings(path)
        self.assertEqual(postings, [])
        self.assertEqual(drops["bad_date"], 1)

    def test_should_count_rows_missing_a_payee_or_account(self):
        path = self.write_csv([
            ["1", "2025-01-01", "", "", "Expenses:Misc", "10.00 EUR", ""],
            ["2", "2025-01-02", "", "Shop", "", "10.00 EUR", ""],
        ])
        postings, drops = dr.read_postings(path)
        self.assertEqual(postings, [])
        self.assertEqual(drops["no_payee_or_account"], 2)

    def test_should_raise_when_a_required_column_is_missing(self):
        path = self.write_csv([["2025-01-01", "X", "Expenses:Misc"]], header=["date", "description", "account"])
        with self.assertRaises(SystemExit):
            dr.read_postings(path)


class TestMain(TempDirTestCase):
    def test_should_print_a_header_and_one_row_per_candidate(self):
        rows = []
        for i, d in enumerate(monthly_dates(date(2025, 1, 8), 6), start=1):
            rows.append([str(i), d.isoformat(), "", "Netflix", "Expenses:Subscriptions", "12.99 EUR", ""])
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
        self.assertTrue(lines[-1].startswith("# 1 group(s) dropped: seen in under 3 months"))
        self.assertIn("rows dropped while reading: 0 bad date", lines[-1])

    def test_should_collapse_case_variants_through_the_real_read_path(self):
        rows = []
        for i, d in enumerate(monthly_dates(date(2025, 1, 1), 3), start=1):
            rows.append([str(i), d.isoformat(), "", "NETFLIX", "Expenses:Subscriptions", "10.00 EUR", ""])
        for i, d in enumerate(monthly_dates(date(2025, 1, 15), 3), start=10):
            rows.append([str(i), d.isoformat(), "", " Netflix ", "Expenses:Subscriptions", "10.00 EUR", ""])
        for i, d in enumerate(monthly_dates(date(2025, 1, 20), 3), start=20):
            rows.append([str(i), d.isoformat(), "", "NETFLIX.COM", "Expenses:Subscriptions", "3.00 EUR", ""])
        path = self.write_csv(rows)

        buf = io.StringIO()
        with redirect_stdout(buf):
            dr.main([path, "--today", "2025-05-01"])
        data = [ln for ln in buf.getvalue().splitlines() if not ln.startswith("#")]
        payees = sorted(ln.split("\t")[1] for ln in data)
        # "NETFLIX" + " Netflix " collapse (6 postings); "NETFLIX.COM" stays separate
        self.assertEqual(payees, ["NETFLIX", "NETFLIX.COM"])
        merged = next(ln for ln in data if ln.split("\t")[1] == "NETFLIX")
        self.assertEqual(dict(zip(dr.COLUMNS, merged.split("\t")))["postings"], "6")

    def test_should_parse_european_decimal_amounts_end_to_end(self):
        rows = [
            [str(i), d.isoformat(), "", "Landlord", "Expenses:Rent", "1.234,56 EUR", ""]
            for i, d in enumerate(monthly_dates(date(2025, 1, 1), 4), start=1)
        ]
        path = self.write_csv(rows)
        buf = io.StringIO()
        with redirect_stdout(buf):
            dr.main([path, "--today", "2025-06-01"])
        data = [ln for ln in buf.getvalue().splitlines() if not ln.startswith("#")]
        self.assertEqual(len(data), 1)
        fields = dict(zip(dr.COLUMNS, data[0].split("\t")))
        self.assertEqual(fields["shape"], "identical")
        self.assertEqual(fields["amount_detail"], "1234.56")
        self.assertEqual(fields["approx_monthly"], "1234.56")

    def test_should_exit_non_zero_on_a_header_only_csv(self):
        path = self.write_csv([])
        with self.assertRaises(SystemExit):
            dr.main([path])

    def test_should_exit_with_a_message_on_a_missing_file(self):
        with self.assertRaises(SystemExit):
            dr.main([str(Path(self._tmp.name) / "nope.csv")])


if __name__ == "__main__":
    unittest.main()
