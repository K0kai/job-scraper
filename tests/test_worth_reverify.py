import tempfile
import unittest
from pathlib import Path
from unittest import mock

import worth_reverify
from job_queue import KIND_LINKEDIN, KIND_WORTH_REVERIFY, JobQueue


class ClassifyHttpTests(unittest.TestCase):
    def test_404_is_inactive(self) -> None:
        self.assertEqual(worth_reverify.classify_http(404, ""), "inactive")

    def test_410_is_inactive(self) -> None:
        self.assertEqual(worth_reverify.classify_http(410, "gone"), "inactive")

    def test_closed_copy_is_inactive(self) -> None:
        body = "Sorry, this job is no longer accepting applications."
        self.assertEqual(worth_reverify.classify_http(200, body), "inactive")

    def test_pt_closed_copy(self) -> None:
        body = "Esta vaga não está mais aceitando candidaturas."
        self.assertEqual(worth_reverify.classify_http(200, body), "inactive")

    def test_apply_signal_is_active(self) -> None:
        body = '<button>Easy Apply</button> Apply now for this role'
        self.assertEqual(worth_reverify.classify_http(200, body), "active")

    def test_ambiguous_is_unknown(self) -> None:
        self.assertEqual(worth_reverify.classify_http(200, "<html>job details</html>"), "unknown")

    def test_linkedin_host_prefers_browser_even_if_http_active(self) -> None:
        self.assertTrue(worth_reverify.needs_browser_check("https://www.linkedin.com/jobs/view/123"))
        self.assertFalse(worth_reverify.needs_browser_check("https://boards.greenhouse.io/acme/jobs/1"))


class ReverifyBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "jobs.db")
        self._init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _connect(self):
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY,
                    source TEXT, source_id TEXT, title TEXT, company TEXT,
                    location TEXT, description TEXT, url TEXT,
                    posted_at TEXT, first_seen_at TEXT, fingerprint TEXT,
                    language TEXT, language_confidence REAL,
                    status TEXT, notes TEXT, applied_at TEXT
                )"""
            )
            for i, (status, url) in enumerate(
                [
                    ("worth", "https://example.com/jobs/open"),
                    ("worth", "https://example.com/jobs/closed"),
                    ("worth", "https://www.linkedin.com/jobs/view/99"),
                    ("ignored", "https://example.com/jobs/old"),
                ],
                start=1,
            ):
                db.execute(
                    """INSERT INTO jobs(id,source,source_id,title,company,location,description,url,
                       posted_at,first_seen_at,fingerprint,language,language_confidence,status,notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        i,
                        "t",
                        f"s{i}",
                        f"T{i}",
                        "C",
                        "R",
                        "d",
                        url,
                        "2026-01-01",
                        "2026-01-01",
                        f"fp{i}",
                        "en",
                        1.0,
                        status,
                        "",
                    ),
                )

    def test_batch_ignores_only_clear_inactive(self) -> None:
        def fake_check(job, **_kwargs):
            url = job["url"]
            if "closed" in url:
                return "inactive", "closed copy"
            if "linkedin" in url:
                return "unknown", "login wall"
            return "active", "ok"

        with mock.patch.object(worth_reverify, "check_job_liveness", side_effect=fake_check):
            summary = worth_reverify.reverify_worth_jobs(self._connect)

        self.assertEqual(summary["checked"], 3)
        self.assertEqual(summary["ignored"], 1)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["unknown"], 1)
        with self._connect() as db:
            statuses = {
                int(r["id"]): r["status"]
                for r in db.execute("SELECT id, status FROM jobs ORDER BY id")
            }
        self.assertEqual(statuses[1], "worth")
        self.assertEqual(statuses[2], "ignored")
        self.assertEqual(statuses[3], "worth")
        self.assertEqual(statuses[4], "ignored")


class ReverifyQueueClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "q.db")
        self.queue = JobQueue(
            db_path=self.db_path,
            handlers={
                KIND_LINKEDIN: lambda payload: "ok",
                KIND_WORTH_REVERIFY: lambda payload: "ok",
                "job_apply": lambda payload: "ok",
            },
            get_settings=lambda: {
                "queue_max_workers": "2",
                "queue_max_attempts": "5",
                "queue_ttl_hours": "24",
            },
        )

    def tearDown(self) -> None:
        self.queue.stop()
        self.tmp.cleanup()

    def test_reverify_not_claimed_while_linkedin_running(self) -> None:
        li = self.queue.enqueue(KIND_LINKEDIN, {"job_id": 1}, dedupe_key="linkedin:1")
        rv = self.queue.enqueue(KIND_WORTH_REVERIFY, {}, dedupe_key="worth-reverify")
        with self.queue._connect() as db:
            db.execute("UPDATE queue_jobs SET status='running' WHERE id=?", (li,))
        batch = self.queue._claim_batch(4)
        kinds = [str(r["kind"]) for r in batch]
        self.assertNotIn(KIND_WORTH_REVERIFY, kinds)
        with self.queue._connect() as db:
            row = db.execute("SELECT status FROM queue_jobs WHERE id=?", (rv,)).fetchone()
        self.assertEqual(row["status"], "pending")

    def test_linkedin_not_claimed_while_reverify_running(self) -> None:
        rv = self.queue.enqueue(KIND_WORTH_REVERIFY, {}, dedupe_key="worth-reverify")
        li = self.queue.enqueue(KIND_LINKEDIN, {"job_id": 2}, dedupe_key="linkedin:2")
        with self.queue._connect() as db:
            db.execute("UPDATE queue_jobs SET status='running' WHERE id=?", (rv,))
        batch = self.queue._claim_batch(4)
        kinds = [str(r["kind"]) for r in batch]
        self.assertNotIn(KIND_LINKEDIN, kinds)
        with self.queue._connect() as db:
            row = db.execute("SELECT status FROM queue_jobs WHERE id=?", (li,)).fetchone()
        self.assertEqual(row["status"], "pending")


if __name__ == "__main__":
    unittest.main()
