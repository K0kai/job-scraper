"""Tabela Vale a pena olhar: sort multi-coluna (ciclo none→asc→desc por header)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class WorthSortCycleTests(unittest.TestCase):
    def test_cycle_keeps_other_columns(self) -> None:
        import app as app_mod

        self.assertEqual(app_mod.cycle_worth_sort("match", ""), "match_asc")
        self.assertEqual(app_mod.cycle_worth_sort("match", "match_asc"), "match_desc")
        self.assertEqual(app_mod.cycle_worth_sort("match", "match_desc"), "")
        # Clicar em seen não apaga match
        both = app_mod.cycle_worth_sort("seen", "match_asc")
        self.assertEqual(both, "seen_asc,match_asc")
        both2 = app_mod.cycle_worth_sort("seen", both)
        self.assertEqual(both2, "seen_desc,match_asc")
        cleared_seen = app_mod.cycle_worth_sort("seen", both2)
        self.assertEqual(cleared_seen, "match_asc")

    def test_combined_order_sql(self) -> None:
        import app as app_mod

        sql = app_mod.worth_order_sql("match_desc,seen_asc")
        self.assertIn("match_score", sql)
        self.assertIn("first_seen_at ASC", sql)
        self.assertTrue(sql.index("match_score") < sql.index("first_seen_at"))
        self.assertEqual(app_mod.normalize_worth_sort("match"), "match_desc")
        self.assertEqual(app_mod._worth_sort_indicator("match_desc,seen_asc", "match"), " ↓")
        self.assertEqual(app_mod._worth_sort_indicator("match_desc,seen_asc", "seen"), " ↑")
        self.assertEqual(app_mod._worth_sort_indicator("match_desc,seen_asc", "posted"), "")


class WorthTableHtmlTests(unittest.TestCase):
    def setUp(self) -> None:
        import app as app_mod

        self.app = app_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = app_mod.DB_PATH
        app_mod.DB_PATH = str(Path(self.tmp.name) / "jobs.db")
        self.addCleanup(lambda: setattr(app_mod, "DB_PATH", self._orig))
        app_mod.initialize()
        brt = ZoneInfo("America/Sao_Paulo")
        seen = datetime(2026, 10, 1, 10, 0, tzinfo=brt).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        with app_mod.connect() as db:
            db.execute(
                """INSERT INTO jobs(source,source_id,title,company,location,description,url,posted_at,first_seen_at,fingerprint,language,language_confidence,status,notes)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "test",
                    "t-1",
                    "Table Job",
                    "Acme",
                    "Remote",
                    "long description here",
                    "https://example.com/t/1",
                    seen,
                    seen,
                    "tfp-1",
                    "en",
                    1.0,
                    "worth",
                    "nota teste",
                ),
            )

    def test_table_layout_single_toolbar_no_select(self) -> None:
        html = self.app.worth_html(page=1, page_size=10, sort="")
        self.assertIn("<table", html)
        self.assertIn('data-worth-sort-col="match"', html)
        self.assertNotIn('id="worth-sort"', html)
        self.assertEqual(html.count('class="worth-filters"'), 1)
        self.assertIn("Table Job", html)

    def test_both_indicators_when_multi_sort(self) -> None:
        html = self.app.worth_html(page=1, page_size=10, sort="match_asc,seen_desc")
        self.assertIn("↑", html)
        self.assertIn("↓", html)


if __name__ == "__main__":
    unittest.main()
