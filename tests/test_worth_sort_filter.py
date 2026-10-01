"""Filtro/ordenação de Vale a pena olhar (helpers legados + combinação)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class WorthSortHelpersTests(unittest.TestCase):
    def test_normalize_and_order_sql(self) -> None:
        import app as app_mod

        self.assertEqual(app_mod.normalize_worth_sort(""), "")
        self.assertEqual(app_mod.normalize_worth_sort("seen_asc"), "seen_asc")
        self.assertEqual(app_mod.normalize_worth_sort("match_asc"), "match_asc")
        self.assertEqual(app_mod.normalize_worth_sort("match"), "match_desc")
        self.assertEqual(app_mod.normalize_worth_sort("nope"), "")
        self.assertIn("DESC", app_mod.worth_order_sql("match_desc"))
        self.assertIn("ASC", app_mod.worth_order_sql("match_asc"))
        self.assertIn("first_seen_at DESC", app_mod.worth_order_sql("seen_desc"))
        self.assertIn("first_seen_at ASC", app_mod.worth_order_sql("seen_asc"))

    def test_date_bounds_utc_brasilia(self) -> None:
        import app as app_mod

        lo, hi = app_mod.worth_date_bounds_utc("2026-10-01", "2026-10-01")
        self.assertIsNotNone(lo)
        self.assertIsNotNone(hi)
        self.assertTrue(str(lo).startswith("2026-10-01T03:00:00"))
        self.assertTrue(str(hi).startswith("2026-10-02T03:00:00"))


class WorthSortFilterIntegrationTests(unittest.TestCase):
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
        stamps = [
            datetime(2026, 9, 28, 12, 0, tzinfo=brt).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),
            datetime(2026, 9, 30, 12, 0, tzinfo=brt).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),
            datetime(2026, 10, 1, 10, 0, tzinfo=brt).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),
        ]
        scores = [90, 50, 70]
        with app_mod.connect() as db:
            for i, (seen, score) in enumerate(zip(stamps, scores)):
                db.execute(
                    """INSERT INTO jobs(source,source_id,title,company,location,description,url,posted_at,first_seen_at,fingerprint,language,language_confidence,status,notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "test",
                        f"s-{i}",
                        f"Seen {i}",
                        "Co",
                        "Remote",
                        "desc",
                        f"https://example.com/s/{i}",
                        seen,
                        seen,
                        f"sfp-{i}",
                        "en",
                        1.0,
                        "worth",
                        "",
                    ),
                )
                job_id = db.execute("SELECT id FROM jobs WHERE source_id=?", (f"s-{i}",)).fetchone()["id"]
                db.execute(
                    """INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason,resume_language,apply_channel,apply_result)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (job_id, app_mod.now_iso(), score, 1, 0, "ok", "en", "", ""),
                )

    def test_sort_seen_and_match(self) -> None:
        desc = self.app.worth_html(page=1, page_size=10, sort="seen_desc")
        asc = self.app.worth_html(page=1, page_size=10, sort="seen_asc")
        self.assertLess(desc.index("Seen 2"), desc.index("Seen 0"))
        self.assertLess(asc.index("Seen 0"), asc.index("Seen 2"))
        match_desc = self.app.worth_html(page=1, page_size=10, sort="match_desc")
        match_asc = self.app.worth_html(page=1, page_size=10, sort="match_asc")
        self.assertLess(match_desc.index("Seen 0"), match_desc.index("Seen 1"))
        self.assertLess(match_asc.index("Seen 1"), match_asc.index("Seen 0"))
        self.assertIn("↑", match_asc)
        self.assertIn("↓", match_desc)

    def test_date_range_with_min_match_combined(self) -> None:
        html = self.app.worth_html(
            page=1,
            page_size=10,
            sort="seen_desc",
            date_from="2026-09-28",
            date_to="2026-10-01",
            min_match=60,
        )
        self.assertIn("Seen 0", html)
        self.assertIn("Seen 2", html)
        self.assertNotIn("Seen 1", html)
        self.assertIn("match ≥ 60", html)


if __name__ == "__main__":
    unittest.main()
