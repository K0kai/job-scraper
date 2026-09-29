import tempfile
import unittest
from pathlib import Path

from job_queue import JobQueue, backoff_seconds, is_retryable_error


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

    def test_database_locked_is_retryable(self) -> None:
        self.assertTrue(is_retryable_error(Exception("OperationalError: database is locked")))

    def test_backoff_jitter_spreads_and_caps(self) -> None:
        samples = [backoff_seconds(1, rate_limited=True) for _ in range(40)]
        self.assertTrue(min(samples) >= 60)
        self.assertTrue(max(samples) <= 20 * 60)
        self.assertGreater(len(set(samples)), 1)
        late = [backoff_seconds(10, rate_limited=True) for _ in range(20)]
        self.assertTrue(all(s <= 20 * 60 for s in late))


if __name__ == "__main__":
    unittest.main()
