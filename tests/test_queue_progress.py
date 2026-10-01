import tempfile
import unittest
from pathlib import Path

from job_queue import JobQueue
from queue_progress import make_progress_fn, noop_progress, progress_from_ai


class QueueProgressHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "q.db")
        self.queue = JobQueue(
            db_path=self.db_path,
            handlers={"job_apply": lambda payload: "ok"},
            get_settings=lambda: {
                "queue_max_workers": "1",
                "queue_max_attempts": "3",
                "queue_ttl_hours": "24",
            },
        )

    def tearDown(self) -> None:
        self.queue.stop()
        self.tmp.cleanup()

    def test_make_progress_fn_appends(self) -> None:
        jid = self.queue.enqueue("job_apply", {"job_id": 1})
        fn = make_progress_fn(self.queue, jid)
        fn("etapa A")
        fn("etapa B")
        with self.queue._connect() as db:
            log = db.execute(
                "SELECT progress_log FROM queue_jobs WHERE id=?", (jid,)
            ).fetchone()["progress_log"]
        self.assertIn("etapa A", log)
        self.assertIn("etapa B", log)

    def test_progress_from_ai(self) -> None:
        seen: list[str] = []
        fn = progress_from_ai({"progress": seen.append})
        fn("x")
        self.assertEqual(seen, ["x"])
        self.assertIs(progress_from_ai({}), noop_progress)
        self.assertIs(progress_from_ai(None), noop_progress)


if __name__ == "__main__":
    unittest.main()
