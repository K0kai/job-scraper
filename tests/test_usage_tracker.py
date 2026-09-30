"""Testes do rastreador de uso Apify + estimativa local de IA."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

import usage_tracker as ut


class AiUsageLocalTests(unittest.TestCase):
    def test_bump_and_summarize_day_month(self) -> None:
        store: dict = {}
        now = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
        ut.record_ai_call(
            store,
            provider="gemini",
            model="gemini-2.5-flash",
            tokens_in=100,
            tokens_out=40,
            ok=True,
            now=now,
        )
        ut.record_ai_call(
            store,
            provider="gemini",
            model="gemini-2.5-flash",
            tokens_in=50,
            tokens_out=10,
            ok=False,
            quota_error=True,
            now=now,
        )
        # outro provedor não entra no resumo do gemini
        ut.record_ai_call(
            store,
            provider="openai",
            model="gpt-4o-mini",
            tokens_in=999,
            tokens_out=999,
            ok=True,
            now=now,
        )
        summary = ut.summarize_ai_usage(store, provider="gemini", now=now)
        self.assertEqual(summary["day"]["calls_ok"], 1)
        self.assertEqual(summary["day"]["calls_quota_error"], 1)
        self.assertEqual(summary["day"]["tokens_in"], 150)
        self.assertEqual(summary["day"]["tokens_out"], 50)
        self.assertEqual(summary["month"]["calls_ok"], 1)
        self.assertEqual(summary["month"]["tokens_in"], 150)
        self.assertTrue(summary["is_local_estimate"])

    def test_extract_tokens_gemini_and_openai(self) -> None:
        g_in, g_out = ut.extract_tokens(
            "gemini",
            {"usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 7}},
        )
        self.assertEqual((g_in, g_out), (12, 7))
        o_in, o_out = ut.extract_tokens(
            "openai",
            {"usage": {"input_tokens": 20, "output_tokens": 5}},
        )
        self.assertEqual((o_in, o_out), (20, 5))


class ApifyQuotaParseTests(unittest.TestCase):
    def test_parse_limits_payload(self) -> None:
        payload = {
            "data": {
                "monthlyUsageCycle": {
                    "startAt": "2026-09-01T00:00:00.000Z",
                    "endAt": "2026-09-30T23:59:59.999Z",
                },
                "limits": {"maxMonthlyUsageUsd": 5},
                "current": {"monthlyUsageUsd": 1.25},
            }
        }
        snap = ut.parse_apify_limits(payload, local_limit_usd=5.0)
        self.assertEqual(snap["used_usd"], 1.25)
        self.assertEqual(snap["api_limit_usd"], 5.0)
        self.assertEqual(snap["local_limit_usd"], 5.0)
        self.assertIn("2026-09-01", snap["cycle_label"])
        self.assertAlmostEqual(snap["pct_of_local"], 25.0)


if __name__ == "__main__":
    unittest.main()
