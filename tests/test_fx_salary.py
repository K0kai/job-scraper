"""FX + conversão salarial para moeda da vaga."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest import mock

import fx_rates
from form_rules import detect_salary_currency, resolve_rule_value, salary_for


class FxRatesTests(unittest.TestCase):
    def setUp(self) -> None:
        fx_rates.clear_memory_cache()

    def tearDown(self) -> None:
        fx_rates.clear_memory_cache()

    def test_convert_usd_to_cop(self) -> None:
        rates = {"USD": 1.0, "COP": 4000.0, "BRL": 5.0}
        out = fx_rates.convert_amount(2000, "USD", "COP", rates=rates)
        self.assertEqual(out, 8_000_000.0)

    def test_convert_brl_to_cop_via_usd(self) -> None:
        rates = {"USD": 1.0, "COP": 4000.0, "BRL": 5.0}
        # 10_000 BRL / 5 = 2000 USD * 4000 = 8_000_000 COP
        out = fx_rates.convert_amount(10_000, "BRL", "COP", rates=rates)
        self.assertEqual(out, 8_000_000.0)

    def test_cache_avoids_refetch(self) -> None:
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return {"USD": 1.0, "COP": 4100.0, "BRL": 5.2}

        cfg: dict = {}
        now = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
        fx_rates.get_usd_rates(cfg, fetch_fn=fetch, now=now, persist=lambda *_: None)
        fx_rates.get_usd_rates(cfg, fetch_fn=fetch, now=now + 60, persist=lambda *_: None)
        self.assertEqual(calls["n"], 1)
        self.assertIn("COP", json.loads(cfg[fx_rates.FX_JSON_KEY]))


class SalaryCurrencyDetectTests(unittest.TestCase):
    def test_cop_from_hint(self) -> None:
        self.assertEqual(detect_salary_currency("Expected salary (COP)"), "COP")
        self.assertEqual(detect_salary_currency("pretensión en pesos colombianos"), "COP")

    def test_eur_and_brl(self) -> None:
        self.assertEqual(detect_salary_currency("Salary in EUR"), "EUR")
        self.assertEqual(detect_salary_currency("R$ 0.000,00"), "BRL")


class SalaryForFxTests(unittest.TestCase):
    def setUp(self) -> None:
        fx_rates.clear_memory_cache()

    def tearDown(self) -> None:
        fx_rates.clear_memory_cache()

    def test_salary_for_cop_from_usd(self) -> None:
        cfg = {"salary_expectation_usd": "2000", "salary_expectation_brl": ""}
        with mock.patch(
            "fx_rates.get_usd_rates",
            return_value={"USD": 1.0, "COP": 4000.0, "BRL": 5.0},
        ):
            value = salary_for(cfg, "COP")
        self.assertTrue(value.startswith("COP "))
        self.assertIn("8.000.000", value)

    def test_resolve_rule_salary_cop(self) -> None:
        rule = {"mode": "salary", "value": "", "value_from": "", "key": "salary"}
        cfg = {"salary_expectation_usd": "1500"}
        with mock.patch(
            "fx_rates.get_usd_rates",
            return_value={"USD": 1.0, "COP": 4000.0, "BRL": 5.0},
        ):
            value = resolve_rule_value(
                rule, cfg, field_hint="Salary expectation in Colombian pesos", job_text=""
            )
        self.assertIsNotNone(value)
        self.assertIn("COP", value or "")
        self.assertIn("6.000.000", value or "")


if __name__ == "__main__":
    unittest.main()
