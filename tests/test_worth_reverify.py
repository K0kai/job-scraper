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

    def test_linkedin_applicants_copy_is_inactive(self) -> None:
        # Texto real do LinkedIn usa "applicants", não "applications".
        body = "No longer accepting applicants"
        self.assertEqual(worth_reverify.classify_http(200, body), "inactive")

    def test_closed_banner_beats_related_easy_apply(self) -> None:
        # Página fechada ainda lista Easy Apply em vagas similares no mesmo HTML.
        body = (
            "No longer accepting applicants"
            '<button aria-label="Easy Apply to Similar Role">Easy Apply</button>'
        )
        self.assertEqual(worth_reverify.classify_http(200, body), "inactive")

    def test_pt_closed_copy(self) -> None:
        body = "Esta vaga não está mais aceitando candidaturas."
        self.assertEqual(worth_reverify.classify_http(200, body), "inactive")

    def test_pt_estamos_mais_aceitando_is_inactive(self) -> None:
        body = "Não estamos mais aceitando candidaturas."
        self.assertEqual(worth_reverify.classify_http(200, body), "inactive")

    def test_pt_closed_banner_beats_candidatura_simplificada(self) -> None:
        body = (
            "Não estamos mais aceitando candidaturas."
            '<button>Candidatura simplificada</button>'
        )
        self.assertEqual(worth_reverify.classify_http(200, body), "inactive")

    def test_pt_candidaturas_encerradas_is_inactive(self) -> None:
        self.assertEqual(
            worth_reverify.classify_http(200, "Candidaturas encerradas"),
            "inactive",
        )

    def test_pt_nao_aceita_mais_candidaturas_is_inactive(self) -> None:
        # Texto real do banner LinkedIn PT (não "não estamos mais aceitando").
        body = (
            '<figcaption class="closed-job__flavor--closed">'
            "Não aceita mais candidaturas</figcaption>"
            "<button>Candidatura simplificada</button>"
        )
        self.assertEqual(worth_reverify.classify_http(200, body), "inactive")

    def test_closed_job_css_marker_is_inactive(self) -> None:
        body = '<figure class="closed-job closed-job__flavor topcard__flavor-row"></figure>'
        self.assertEqual(worth_reverify.classify_http(200, body), "inactive")

    def test_apply_signal_is_active(self) -> None:
        body = '<button>Easy Apply</button> Apply now for this role'
        self.assertEqual(worth_reverify.classify_http(200, body), "active")

    def test_ambiguous_is_unknown(self) -> None:
        self.assertEqual(worth_reverify.classify_http(200, "<html>job details</html>"), "unknown")

    def test_linkedin_host_prefers_browser_even_if_http_active(self) -> None:
        self.assertTrue(worth_reverify.needs_browser_check("https://www.linkedin.com/jobs/view/123"))
        self.assertFalse(worth_reverify.needs_browser_check("https://boards.greenhouse.io/acme/jobs/1"))


class LinkedInGuestTests(unittest.TestCase):
    def test_extract_job_id_from_view_url(self) -> None:
        self.assertEqual(
            worth_reverify.extract_linkedin_job_id(
                "https://www.linkedin.com/jobs/view/2000000000/"
            ),
            "2000000000",
        )
        self.assertEqual(
            worth_reverify.extract_linkedin_job_id(
                "https://www.linkedin.com/jobs/view/acme-dev-4192503234?refId=x"
            ),
            "4192503234",
        )

    def test_guest_closed_pt_banner(self) -> None:
        html = (
            '<figure class="closed-job">'
            '<figcaption class="closed-job__flavor--closed">'
            "Não aceita mais candidaturas</figcaption></figure>"
        )
        with mock.patch.object(
            worth_reverify, "fetch_url_text", return_value=(200, html)
        ):
            verdict, detail = worth_reverify.check_linkedin_guest_liveness("2000000000")
        self.assertEqual(verdict, "inactive")
        self.assertIn("fechada", detail)

    def test_guest_active_apply_marker(self) -> None:
        html = '<a class="public_jobs_apply-link-onsite topcard-apply" href="#">Apply</a>'
        with mock.patch.object(
            worth_reverify, "fetch_url_text", return_value=(200, html)
        ):
            verdict, _detail = worth_reverify.check_linkedin_guest_liveness("4192503234")
        self.assertEqual(verdict, "active")

    def test_check_job_prefers_guest_over_browser(self) -> None:
        job = {"id": 1, "url": "https://www.linkedin.com/jobs/view/2000000000"}
        with mock.patch.object(
            worth_reverify,
            "check_linkedin_guest_liveness",
            return_value=("inactive", "guest: vaga fechada"),
        ) as guest, mock.patch.object(
            worth_reverify, "LinkedInLivenessSession"
        ) as session_cls:
            verdict, detail = worth_reverify.check_job_liveness(job)
        self.assertEqual(verdict, "inactive")
        self.assertIn("guest", detail)
        guest.assert_called_once()
        session_cls.assert_not_called()


class BrowserWaitTests(unittest.TestCase):
    def test_wait_returns_inactive_when_closed_copy_appears(self) -> None:
        class FakePage:
            def __init__(self):
                self.n = 0
                self.url = "https://www.linkedin.com/jobs/view/1"

            def content(self):
                self.n += 1
                if self.n < 3:
                    return "<html>loading</html>"
                return "<html>This job is no longer accepting applications.</html>"

            def wait_for_timeout(self, ms):
                return None

        verdict, _detail = worth_reverify.wait_page_liveness_signal(
            FakePage(), timeout_ms=5_000, poll_ms=1
        )
        self.assertEqual(verdict, "inactive")

    def test_wait_returns_active_on_easy_apply(self) -> None:
        class FakePage:
            url = "https://www.linkedin.com/jobs/view/2"

            def content(self):
                return '<button aria-label="Easy Apply">Easy Apply</button>'

            def wait_for_timeout(self, ms):
                return None

        with mock.patch.object(worth_reverify, "ACTIVE_CONFIRM_GRACE_MS", 30):
            verdict, _detail = worth_reverify.wait_page_liveness_signal(
                FakePage(), timeout_ms=2_000, poll_ms=1
            )
        self.assertEqual(verdict, "active")

    def test_wait_prefers_late_closed_banner_over_early_easy_apply(self) -> None:
        """LinkedIn hidrata Easy Apply de similares antes do banner 'fechada'."""

        class FakePage:
            def __init__(self):
                self.n = 0
                self.url = "https://www.linkedin.com/jobs/view/3"

            def content(self):
                self.n += 1
                if self.n < 4:
                    return '<html><button>Easy Apply</button> similares</html>'
                return (
                    "<html>No longer accepting applicants"
                    "<button>Easy Apply</button></html>"
                )

            def wait_for_timeout(self, ms):
                return None

        verdict, _detail = worth_reverify.wait_page_liveness_signal(
            FakePage(), timeout_ms=5_000, poll_ms=1
        )
        self.assertEqual(verdict, "inactive")

    def test_wait_confirms_active_after_grace(self) -> None:
        class FakePage:
            url = "https://www.linkedin.com/jobs/view/4"

            def content(self):
                return '<button>Easy Apply</button>'

            def wait_for_timeout(self, ms):
                return None

        with mock.patch.object(worth_reverify, "ACTIVE_CONFIRM_GRACE_MS", 30):
            verdict, _detail = worth_reverify.wait_page_liveness_signal(
                FakePage(), timeout_ms=2_000, poll_ms=1
            )
        self.assertEqual(verdict, "active")

    def test_wait_returns_unknown_on_linkedin_load_error(self) -> None:
        class FakePage:
            url = "https://www.linkedin.com/jobs/view/9"

            def content(self):
                return "<html>Não foi possível carregar a página. Tente novamente.</html>"

            def wait_for_timeout(self, ms):
                raise AssertionError("não deve esperar timeout em load error")

        verdict, detail = worth_reverify.wait_page_liveness_signal(
            FakePage(), timeout_ms=25_000, poll_ms=500
        )
        self.assertEqual(verdict, "unknown")
        self.assertIn("carregar", detail.casefold())

    def test_check_url_retries_once_on_load_error(self) -> None:
        calls = {"goto": 0, "reload": 0}

        class FakePage:
            url = "https://www.linkedin.com/jobs/view/9"

            def goto(self, url, **kwargs):
                calls["goto"] += 1

            def reload(self, **kwargs):
                calls["reload"] += 1

            def content(self):
                if calls["reload"] == 0:
                    return "<html>Não foi possível carregar a página</html>"
                return "<button>Easy Apply</button>"

            def wait_for_timeout(self, ms):
                return None

            class mouse:
                @staticmethod
                def wheel(*a, **k):
                    return None

        session = worth_reverify.LinkedInLivenessSession.__new__(
            worth_reverify.LinkedInLivenessSession
        )
        session._page = FakePage()
        session.goto_timeout_ms = 1000
        session.page_timeout_ms = 2000
        session.should_abort = None
        verdict, _detail = session.check_url("https://www.linkedin.com/jobs/view/9")
        self.assertEqual(calls["reload"], 1)
        self.assertEqual(verdict, "active")

    def test_wait_unknown_on_timeout(self) -> None:
        class FakePage:
            url = "https://www.linkedin.com/jobs/view/3"

            def content(self):
                return "<html>still loading shell</html>"

            def wait_for_timeout(self, ms):
                return None

        verdict, detail = worth_reverify.wait_page_liveness_signal(
            FakePage(), timeout_ms=5, poll_ms=1
        )
        self.assertEqual(verdict, "unknown")
        self.assertIn("timeout", detail.casefold())


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

    def test_batch_reuses_one_browser_session_for_linkedin(self) -> None:
        """Várias vagas LI não devem abrir/fechar Chrome por URL."""
        with self._connect() as db:
            db.execute("DELETE FROM jobs")
            for i in (1, 2, 3):
                db.execute(
                    """INSERT INTO jobs(id,source,source_id,title,company,location,description,url,
                       posted_at,first_seen_at,fingerprint,language,language_confidence,status,notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        i,
                        "t",
                        f"li{i}",
                        f"T{i}",
                        "C",
                        "R",
                        "d",
                        f"https://www.linkedin.com/jobs/view/{i}",
                        "2026-01-01",
                        "2026-01-01",
                        f"fp{i}",
                        "en",
                        1.0,
                        "worth",
                        "",
                    ),
                )

        launches = {"n": 0}
        checks = {"urls": []}

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                launches["n"] += 1
                return self

            def __exit__(self, *args):
                return False

            def check_url(self, url):
                checks["urls"].append(url)
                return "active", "browser"

        with mock.patch.object(
            worth_reverify, "LinkedInLivenessSession", FakeSession
        ), mock.patch.object(
            worth_reverify, "check_linkedin_guest_liveness", return_value=("unknown", "guest skip")
        ), mock.patch.object(
            worth_reverify, "check_http_liveness", return_value=("unknown", "HTTP 999")
        ):
            summary = worth_reverify.reverify_worth_jobs(self._connect)

        self.assertEqual(launches["n"], 1)
        self.assertEqual(len(checks["urls"]), 3)
        self.assertEqual(summary["active"], 3)

    def test_batch_stops_when_aborted(self) -> None:
        seen = []

        def fake_check(job, **_kwargs):
            seen.append(int(job["id"]))
            return "active", "ok"

        calls = {"n": 0}

        def abort_after_one() -> bool:
            calls["n"] += 1
            return calls["n"] > 1

        with mock.patch.object(worth_reverify, "check_job_liveness", side_effect=fake_check):
            summary = worth_reverify.reverify_worth_jobs(
                self._connect, should_abort=abort_after_one
            )

        self.assertEqual(len(seen), 1)
        self.assertTrue(summary.get("aborted"))
        self.assertEqual(summary["checked"], 1)

    def test_batch_ignores_only_clear_inactive(self) -> None:
        def fake_check(job, **_kwargs):
            url = job["url"]
            if "closed" in url:
                return "inactive", "closed copy"
            if "linkedin" in url:
                return "unknown", "login wall"
            return "active", "ok"

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def check_url(self, url):
                return fake_check({"url": url})

        with mock.patch.object(worth_reverify, "check_job_liveness", side_effect=fake_check), mock.patch.object(
            worth_reverify, "LinkedInLivenessSession", FakeSession
        ):
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
