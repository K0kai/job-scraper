"""Tier de vaga + cap de pretensão para estágio/júnior."""
from __future__ import annotations

import unittest

from salary_policy import (
    apply_role_tier_cap,
    detect_role_tier,
    propose_salary,
)


class RoleTierTests(unittest.TestCase):
    def test_intern(self) -> None:
        self.assertEqual(detect_role_tier("Estágio em Python"), "intern")
        self.assertEqual(detect_role_tier("Software Engineering Intern"), "intern")
        self.assertEqual(detect_role_tier("Trainee de dados"), "intern")

    def test_junior(self) -> None:
        self.assertEqual(detect_role_tier("Desenvolvedor Júnior"), "junior")
        self.assertEqual(detect_role_tier("Junior Backend Engineer"), "junior")

    def test_standard(self) -> None:
        self.assertEqual(detect_role_tier("Senior Software Engineer"), "standard")
        self.assertEqual(detect_role_tier("Engenheiro Pleno"), "standard")


class RoleTierCapTests(unittest.TestCase):
    def test_intern_caps_brl_monthly(self) -> None:
        self.assertEqual(
            apply_role_tier_cap(2000, currency="BRL", period="month", tier="intern"),
            1200.0,
        )

    def test_standard_unchanged(self) -> None:
        self.assertEqual(
            apply_role_tier_cap(2000, currency="BRL", period="month", tier="standard"),
            2000.0,
        )


class ProposeSalaryInternTests(unittest.TestCase):
    def test_internship_brl_caps_panel_expectation(self) -> None:
        cfg = {
            "salary_expectation_brl": "2000",
            "salary_expectation_usd": "",
            "candidate_contract_type": "employee",
        }
        prop = propose_salary(
            cfg,
            field_hint="Pretensão salarial mensal",
            job_text="Estágio em desenvolvimento de software — Belo Horizonte",
        )
        self.assertEqual(prop["tier"], "intern")
        self.assertEqual(prop["amount"], 1200.0)
        self.assertIn("1.200", prop["formatted"])


if __name__ == "__main__":
    unittest.main()
