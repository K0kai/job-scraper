"""Smoke do painel Uso & cotas."""
from __future__ import annotations

import unittest
from unittest import mock


class UsageDashboardHtmlTests(unittest.TestCase):
    def test_html_marks_local_estimate_and_active_provider(self) -> None:
        import app

        with mock.patch.object(
            app,
            "settings",
            return_value={
                "ai_provider": "openai",
                "ai_model": "gpt-4o-mini",
                "ai_usage_json": "",
                "apify_quota_snapshot_json": "",
                "apify_monthly_credit_limit_usd": "5",
            },
        ), mock.patch.object(app, "secret_get", return_value=""):
            html = app.usage_dashboard_html()
        self.assertIn("Estimativa local deste app", html)
        self.assertIn("OpenAI", html)
        self.assertIn("Apify", html)
        self.assertIn("/usage-refresh", html)


if __name__ == "__main__":
    unittest.main()
