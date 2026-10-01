"""Testes de período / normalização / proposta salarial."""
from __future__ import annotations

import unittest
from unittest import mock

import salary_policy
from salary_policy import detect_salary_period, normalize_amount, propose_salary


class DetectPeriodTests(unittest.TestCase):
    def test_year_month_hour(self) -> None:
        self.assertEqual(detect_salary_period("Expected annual salary (USD)"), "year")
        self.assertEqual(detect_salary_period("Pretensão mensal"), "month")
        self.assertEqual(detect_salary_period("Rate per hour"), "hour")
        self.assertEqual(detect_salary_period("Expected salary"), "unknown")


class NormalizeTests(unittest.TestCase):
    def test_month_year_hour(self) -> None:
        self.assertEqual(normalize_amount(10_000, "month", "year"), 120_000)
        self.assertEqual(normalize_amount(120_000, "year", "month"), 10_000)
        self.assertAlmostEqual(
            normalize_amount(120_000, "year", "hour"),
            120_000 / salary_policy.HOURS_PER_YEAR,
        )


class ProposeTests(unittest.TestCase):
    def test_annual_field_multiplies_monthly_panel(self) -> None:
        cfg = {"salary_expectation_usd": "5000", "salary_expectation_brl": ""}
        with mock.patch("salary_policy.salary_for", return_value="$ 5,000"):
            out = propose_salary(cfg, field_hint="Annual salary expectation USD")
        self.assertEqual(out["period"], "year")
        self.assertEqual(out["amount"], 60_000)
        self.assertIn("60", out["formatted"].replace(",", ""))


if __name__ == "__main__":
    unittest.main()
