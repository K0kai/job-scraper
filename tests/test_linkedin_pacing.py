"""Pacing Easy Apply: só intervalo mínimo (sem limite diário)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from linkedin_apply import (
    DEFAULT_MIN_GAP_MINUTES,
    HARD_MAX_GAP_MINUTES,
    HARD_MIN_GAP_MINUTES,
    rate_limit_block_reason,
)


class LinkedInPacingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "t.db")
        from dbutil import open_connection

        with open_connection(self.db_path) as db:
            db.execute(
                """CREATE TABLE applications (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER,
                    channel TEXT,
                    attempted_at TEXT,
                    status TEXT
                )"""
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _connect(self):
        from dbutil import open_connection

        return open_connection(self.db_path)

    def _insert_linkedin(self, *, minutes_ago: float) -> None:
        when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        with self._connect() as db:
            db.execute(
                "INSERT INTO applications(job_id, channel, attempted_at, status) VALUES (?,?,?,?)",
                (1, "linkedin", when.isoformat(timespec="seconds"), "ok"),
            )

    def test_gap_constants_option_a(self) -> None:
        self.assertEqual(HARD_MIN_GAP_MINUTES, 1)
        self.assertEqual(DEFAULT_MIN_GAP_MINUTES, 3)
        self.assertEqual(HARD_MAX_GAP_MINUTES, 30)

    def test_gap_blocks_when_too_soon(self) -> None:
        self._insert_linkedin(minutes_ago=1)
        reason = rate_limit_block_reason(self._connect, {"linkedin_min_gap_minutes": "3"})
        self.assertIsNotNone(reason)
        self.assertIn("Intervalo mínimo", reason or "")

    def test_no_daily_cap_even_with_many_today(self) -> None:
        for i in range(20):
            when = datetime.now(timezone.utc) - timedelta(hours=i % 5, minutes=30)
            with self._connect() as db:
                db.execute(
                    "INSERT INTO applications(job_id, channel, attempted_at, status) VALUES (?,?,?,?)",
                    (i, "linkedin", when.isoformat(timespec="seconds"), "ok"),
                )
        # última ação há muito tempo → gap ok; daily não deve bloquear
        with mock.patch("linkedin_apply.last_linkedin_action_at", return_value=None):
            reason = rate_limit_block_reason(
                self._connect,
                {"linkedin_min_gap_minutes": "3", "linkedin_max_per_day": "3"},
            )
        self.assertIsNone(reason)

    def test_gap_allows_after_wait(self) -> None:
        self._insert_linkedin(minutes_ago=10)
        reason = rate_limit_block_reason(self._connect, {"linkedin_min_gap_minutes": "3"})
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
