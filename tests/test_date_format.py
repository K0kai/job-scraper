import unittest
from datetime import datetime, timezone


class DateFormatTests(unittest.TestCase):
    def test_history_uses_brasilia_not_raw_utc(self) -> None:
        import app as app_mod

        # 18:30 UTC = 15:30 em Brasília (UTC-3).
        utc = datetime(2026, 3, 15, 18, 30, tzinfo=timezone.utc).isoformat(timespec="seconds")
        html = app_mod.history_html([{"started_at": utc, "message": "ok", "state": "completed"}])
        self.assertIn("15/03/2026 15:30", html)
        self.assertNotIn("2026-03-15 18:30", html)

    def test_job_list_date_is_dd_mm_yyyy(self) -> None:
        import app as app_mod

        utc = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc).isoformat(timespec="seconds")
        self.assertEqual(app_mod.format_brasilia_date(utc), "29/09/2026")
        self.assertEqual(app_mod.format_brasilia_date("2026-01-05"), "05/01/2026")


if __name__ == "__main__":
    unittest.main()
