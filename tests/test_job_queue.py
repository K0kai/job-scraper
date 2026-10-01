import tempfile
import unittest
from pathlib import Path

from job_queue import KIND_APPLY, KIND_LINKEDIN, JobQueue, backoff_seconds, is_retryable_error


class QueueRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "q.db")
        self.queue = JobQueue(
            db_path=self.db_path,
            handlers={"job_apply": lambda payload: "ok"},
            get_settings=lambda: {
                "queue_max_workers": "2",
                "queue_max_attempts": "5",
                "queue_ttl_hours": "24",
            },
        )

    def tearDown(self) -> None:
        self.queue.stop()
        self.tmp.cleanup()

    def _insert(self, *, status: str, last_error: str = "") -> int:
        jid = self.queue.enqueue("job_apply", {"job_id": 1}, dedupe_key=None)
        with self.queue._connect() as db:
            db.execute(
                "UPDATE queue_jobs SET status=?, last_error=?, attempts=3 WHERE id=?",
                (status, last_error, jid),
            )
        return jid

    def test_recover_stale_running_on_start(self) -> None:
        jid = self._insert(status="running", last_error="OperationalError: database is locked")
        n = self.queue.recover_stale_running()
        self.assertEqual(n, 1)
        with self.queue._connect() as db:
            row = db.execute("SELECT status, attempts FROM queue_jobs WHERE id=?", (jid,)).fetchone()
        self.assertEqual(row["status"], "pending")

    def test_retry_now_failed_refreshes_ttl_and_attempts(self) -> None:
        jid = self._insert(status="failed", last_error="database is locked")
        self.assertTrue(self.queue.retry_now(jid))
        with self.queue._connect() as db:
            row = db.execute(
                "SELECT status, attempts, last_error FROM queue_jobs WHERE id=?", (jid,)
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["last_error"], "")

    def test_retry_now_rejects_succeeded(self) -> None:
        jid = self._insert(status="succeeded")
        self.assertFalse(self.queue.retry_now(jid))

    def test_retry_all_batch(self) -> None:
        a = self._insert(status="failed")
        b = self._insert(status="cancelled")
        c = self._insert(status="running")
        n = self.queue.retry_all()
        self.assertEqual(n, 3)
        with self.queue._connect() as db:
            statuses = {
                int(r["id"]): r["status"]
                for r in db.execute("SELECT id, status FROM queue_jobs WHERE id IN (?,?,?)", (a, b, c))
            }
        self.assertEqual(statuses[a], "pending")
        self.assertEqual(statuses[b], "pending")
        self.assertEqual(statuses[c], "pending")

    def test_finish_does_not_overwrite_cancelled(self) -> None:
        jid = self.queue.enqueue("job_apply", {"job_id": 1}, dedupe_key=None)
        with self.queue._connect() as db:
            db.execute("UPDATE queue_jobs SET status='running' WHERE id=?", (jid,))
        self.assertTrue(self.queue.cancel(jid))
        self.queue._finish(jid, "succeeded", 1, "", "should-not-stick")
        with self.queue._connect() as db:
            row = db.execute("SELECT status, result FROM queue_jobs WHERE id=?", (jid,)).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertNotEqual(row["result"], "should-not-stick")

    def test_is_cancelled(self) -> None:
        jid = self.queue.enqueue("job_apply", {"job_id": 1}, dedupe_key=None)
        self.assertFalse(self.queue.is_cancelled(jid))
        with self.queue._connect() as db:
            db.execute("UPDATE queue_jobs SET status='running' WHERE id=?", (jid,))
        self.queue.cancel(jid)
        self.assertTrue(self.queue.is_cancelled(jid))

    def test_backoff_jitter_spreads_and_caps(self) -> None:
        samples = [backoff_seconds(1, rate_limited=True) for _ in range(40)]
        self.assertTrue(min(samples) >= 60)
        self.assertTrue(max(samples) <= 20 * 60)
        self.assertGreater(len(set(samples)), 1)
        late = [backoff_seconds(10, rate_limited=True) for _ in range(20)]
        self.assertTrue(all(s <= 20 * 60 for s in late))

    def test_append_progress_survives_finish(self) -> None:
        jid = self.queue.enqueue("job_apply", {"job_id": 1}, dedupe_key=None)
        with self.queue._connect() as db:
            db.execute("UPDATE queue_jobs SET status='running' WHERE id=?", (jid,))
        self.queue.append_progress(jid, "abrindo Chrome")
        self.queue.append_progress(jid, "preenchendo passo 1")
        self.queue._finish(jid, "succeeded", 1, "", "vaga 1: ok")
        with self.queue._connect() as db:
            row = db.execute(
                "SELECT status, result, last_error, progress_log FROM queue_jobs WHERE id=?",
                (jid,),
            ).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["result"], "vaga 1: ok")
        self.assertEqual(row["last_error"], "")
        log = row["progress_log"] or ""
        self.assertIn("abrindo Chrome", log)
        self.assertIn("preenchendo passo 1", log)
        self.assertTrue(log.index("abrindo Chrome") < log.index("preenchendo passo 1"))

    def test_append_progress_noop_on_bad_id(self) -> None:
        self.queue.append_progress(None, "ignored")
        self.queue.append_progress("x", "ignored")
        self.queue.append_progress(999999, "missing-row")


class LinkedInQueueConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "q.db")
        self.queue = JobQueue(
            db_path=self.db_path,
            handlers={
                KIND_LINKEDIN: lambda payload: "ok",
                KIND_APPLY: lambda payload: "ok",
            },
            get_settings=lambda: {
                "queue_max_workers": "3",
                "queue_max_attempts": "5",
                "queue_ttl_hours": "24",
            },
        )

    def tearDown(self) -> None:
        self.queue.stop()
        self.tmp.cleanup()

    def test_claim_batch_only_one_linkedin_at_a_time(self) -> None:
        a = self.queue.enqueue(KIND_LINKEDIN, {"job_id": 1}, dedupe_key="linkedin:1")
        b = self.queue.enqueue(KIND_LINKEDIN, {"job_id": 2}, dedupe_key="linkedin:2")
        c = self.queue.enqueue(KIND_APPLY, {"job_id": 3}, dedupe_key="apply:3")
        claimed = self.queue._claim_batch(3)
        kinds = [row["kind"] for row in claimed]
        self.assertEqual(kinds.count(KIND_LINKEDIN), 1)
        self.assertIn(KIND_APPLY, kinds)
        claimed_ids = {int(row["id"]) for row in claimed}
        self.assertTrue(claimed_ids & {a, b})
        # segundo linkedin permanece pending
        with self.queue._connect() as db:
            statuses = {
                int(r["id"]): r["status"]
                for r in db.execute(
                    "SELECT id, status FROM queue_jobs WHERE id IN (?,?,?)", (a, b, c)
                )
            }
        linkedin_running = sum(
            1 for jid in (a, b) if statuses[jid] == "running"
        )
        self.assertEqual(linkedin_running, 1)
        self.assertEqual(statuses[c], "running")

    def test_claim_skips_linkedin_when_one_already_running(self) -> None:
        a = self.queue.enqueue(KIND_LINKEDIN, {"job_id": 1}, dedupe_key="linkedin:1")
        b = self.queue.enqueue(KIND_LINKEDIN, {"job_id": 2}, dedupe_key="linkedin:2")
        with self.queue._connect() as db:
            db.execute("UPDATE queue_jobs SET status='running' WHERE id=?", (a,))
        claimed = self.queue._claim_batch(2)
        self.assertEqual(claimed, [])
        with self.queue._connect() as db:
            row = db.execute("SELECT status FROM queue_jobs WHERE id=?", (b,)).fetchone()
        self.assertEqual(row["status"], "pending")


if __name__ == "__main__":
    unittest.main()
