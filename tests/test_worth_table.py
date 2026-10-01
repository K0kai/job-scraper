"""Tabela Vale a pena olhar: sort por coluna (ciclo none→asc→desc)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class WorthSortCycleTests(unittest.TestCase):
    def test_cycle_and_normalize(self) -> None:
        import app as app_mod

        self.assertEqual(app_mod.cycle_worth_sort("match", ""), "match_asc")
        self.assertEqual(app_mod.cycle_worth_sort("match", "match_asc"), "match_desc")
        self.assertEqual(app_mod.cycle_worth_sort("match", "match_desc"), "")
        self.assertEqual(app_mod.cycle_worth_sort("seen", "match_asc"), "seen_asc")
        self.assertEqual(app_mod.normalize_worth_sort(""), "")
        self.assertEqual(app_mod.normalize_worth_sort("match"), "match_desc")  # legado
        self.assertEqual(app_mod.normalize_worth_sort("posted_asc"), "posted_asc")
        self.assertIn("id DESC", app_mod.worth_order_sql(""))
        self.assertIn("match_score", app_mod.worth_order_sql("match_asc"))
        self.assertIn("ASC", app_mod.worth_order_sql("match_asc"))
        self.assertIn("posted_at", app_mod.worth_order_sql("posted_desc"))


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
        self.assertIn('data-worth-sort-col="seen"', html)
        self.assertIn('data-worth-sort-col="posted"', html)
        self.assertNotIn('id="worth-sort"', html)
        self.assertEqual(html.count('class="worth-filters"'), 1)
        self.assertEqual(html.count('class="worth-pager"'), 1)
        self.assertIn("Table Job", html)
        self.assertIn("<details", html)
        self.assertIn("nota teste", html)


if __name__ == "__main__":
    unittest.main()
