import tempfile
import unittest
from pathlib import Path
from unittest import mock


class AutoApplyBatchTests(unittest.TestCase):
    def test_batch_enqueues_all_new_not_just_apply_cap(self) -> None:
        import app as app_mod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "jobs.db")

        original_db = app_mod.DB_PATH
        app_mod.DB_PATH = db_path
        self.addCleanup(lambda: setattr(app_mod, "DB_PATH", original_db))

        app_mod.initialize()
        with app_mod.connect() as db:
            for i in range(12):
                db.execute(
                    """INSERT INTO jobs(source,source_id,title,company,location,description,url,posted_at,first_seen_at,fingerprint,language,language_confidence,status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "test",
                        f"id-{i}",
                        f"Job {i}",
                        "Co",
                        "Remote",
                        "desc",
                        f"https://example.com/{i}",
                        "",
                        app_mod.now_iso(),
                        f"fp-{i}",
                        "en",
                        1.0,
                        "new",
                    ),
                )

        enqueued: list[int] = []

        def fake_enqueue(kind, payload, *, dedupe_key=None):
            enqueued.append(int(payload["job_id"]))
            return len(enqueued)

        with mock.patch.object(app_mod, "settings", return_value={
            "auto_apply": "1",
            "queue_max_workers": "3",
        }), mock.patch.object(app_mod.queue, "enqueue", side_effect=fake_enqueue):
            note = app_mod.process_auto_apply_batch()

        self.assertEqual(len(enqueued), 12)
        self.assertIn("12 vaga(s) enfileirada(s)", note)
        self.assertIn("até 3 worker(s)", note)
        with app_mod.connect() as db:
            left_new = db.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='new'").fetchone()["n"]
            in_review = db.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='review'").fetchone()["n"]
        self.assertEqual(left_new, 0)
        self.assertEqual(in_review, 12)


if __name__ == "__main__":
    unittest.main()
