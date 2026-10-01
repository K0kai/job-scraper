import tempfile
import unittest
from pathlib import Path


class WorthPaginationTests(unittest.TestCase):
    def test_worth_html_paginates(self) -> None:
        import app as app_mod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original_db = app_mod.DB_PATH
        app_mod.DB_PATH = str(Path(tmp.name) / "jobs.db")
        self.addCleanup(lambda: setattr(app_mod, "DB_PATH", original_db))
        app_mod.initialize()
        with app_mod.connect() as db:
            for i in range(18):
                db.execute(
                    """INSERT INTO jobs(source,source_id,title,company,location,description,url,posted_at,first_seen_at,fingerprint,language,language_confidence,status,notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "test",
                        f"w-{i}",
                        f"Worth {i}",
                        "Co",
                        "Remote",
                        "desc",
                        f"https://example.com/w/{i}",
                        "",
                        app_mod.now_iso(),
                        f"wfp-{i}",
                        "en",
                        1.0,
                        "worth",
                        "nota",
                    ),
                )

        page1 = app_mod.worth_html(page=1, page_size=10)
        page2 = app_mod.worth_html(page=2, page_size=10)
        self.assertIn("1–10 de 18", page1)
        self.assertIn("11–18 de 18", page2)
        self.assertIn('data-worth-page="2"', page1)
        self.assertIn("Worth 17", page1)  # highest id first (default)
        self.assertIn("Worth 0", page2)
        self.assertNotIn("Worth 0", page1)

        with app_mod.connect() as db:
            for i, score in enumerate([55, 72, 88, 91]):
                job_id = db.execute(
                    "SELECT id FROM jobs WHERE source_id=?", (f"w-{i}",)
                ).fetchone()["id"]
                db.execute(
                    """INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason,resume_language,apply_channel,apply_result)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (job_id, app_mod.now_iso(), score, 1, 0, "ok", "en", "", ""),
                )
        filtered = app_mod.worth_html(page=1, page_size=20, min_match=80)
        self.assertIn("match ≥ 80", filtered)
        self.assertIn("Worth 2", filtered)
        self.assertIn("Worth 3", filtered)
        self.assertNotIn("Worth 0", filtered)
        self.assertNotIn("Worth 1", filtered)
        self.assertIn("worth-min-match", filtered)
        self.assertIn("<table", filtered)


if __name__ == "__main__":
    unittest.main()
