"""Testes do review salarial (parse + fallback)."""
from __future__ import annotations

import unittest
from unittest import mock

from resume_pipeline import AiUnavailableError
from salary_review import parse_salary_review, review_salary_value


class ParseSalaryReviewTests(unittest.TestCase):
    def test_ok_adjust_ask(self) -> None:
        self.assertEqual(parse_salary_review('{"action":"ok","reason":"fine"}')["action"], "ok")
        adj = parse_salary_review('{"action":"adjust","value":"$ 55,000","period":"year"}')
        self.assertEqual(adj["action"], "adjust")
        self.assertEqual(adj["value"], "$ 55,000")
        ask = parse_salary_review('{"action":"ask","question":"Annual or monthly?"}')
        self.assertEqual(ask["action"], "ask")
        self.assertIn("Annual", ask["question"])

    def test_garbage_defaults_ok(self) -> None:
        self.assertEqual(parse_salary_review("not json")["action"], "ok")


class ReviewSalaryValueTests(unittest.TestCase):
    def test_unavailable_keeps_proposal(self) -> None:
        with mock.patch("ai_client.call_ai_text", side_effect=AiUnavailableError("429")):
            value, meta = review_salary_value(
                proposed_formatted="$ 60,000",
                proposal={"amount": 60000, "period": "year", "currency": "USD"},
                field_hint="Annual salary",
                job_text="US remote",
                ai={"provider": "gemini", "model": "m", "api_key": "k"},
            )
        self.assertEqual(value, "$ 60,000")
        self.assertIn("unavailable", meta["reason"])

    def test_adjust_applies(self) -> None:
        with mock.patch(
            "ai_client.call_ai_text",
            return_value='{"action":"adjust","value":"$ 54,000","reason":"offshore"}',
        ):
            value, meta = review_salary_value(
                proposed_formatted="$ 60,000",
                proposal={"amount": 60000, "period": "year", "currency": "USD"},
                field_hint="Annual salary",
                job_text="Colombia",
                ai={"provider": "gemini", "model": "m", "api_key": "k"},
            )
        self.assertEqual(value, "$ 54,000")
        self.assertEqual(meta["action"], "adjust")

    def test_skip_review_when_period_known_usd(self) -> None:
        from salary_review import needs_salary_ai_review

        self.assertFalse(
            needs_salary_ai_review({"period": "year", "currency": "USD"}, options=None)
        )
        self.assertTrue(
            needs_salary_ai_review({"period": "unknown", "currency": "USD"}, options=None)
        )
        self.assertTrue(
            needs_salary_ai_review(
                {"period": "year", "currency": "USD"}, options=["40k", "50k"]
            )
        )
        self.assertTrue(
            needs_salary_ai_review({"period": "month", "currency": "COP"}, options=None)
        )

    def test_intern_junior_always_review_even_brl(self) -> None:
        from salary_review import needs_salary_ai_review

        self.assertTrue(
            needs_salary_ai_review(
                {"period": "month", "currency": "BRL", "tier": "intern"},
                job_text="Estágio em engenharia de software",
            )
        )
        self.assertTrue(
            needs_salary_ai_review(
                {"period": "month", "currency": "BRL"},
                job_text="Vaga de estágio remunerado",
                field_hint="Pretensão salarial",
            )
        )
        self.assertFalse(
            needs_salary_ai_review(
                {"period": "month", "currency": "BRL", "tier": "standard"},
                job_text="Software Engineer Pleno",
            )
        )


if __name__ == "__main__":
    unittest.main()
