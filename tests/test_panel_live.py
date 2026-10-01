import unittest

import app


class ResumeStatusLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.initialize()

    def test_status_html_has_no_file_input(self):
        html = app.resume_status_html_for("pt")
        self.assertNotIn('type="file"', html)
        self.assertNotIn("force_reanalyze", html)
        self.assertIn("resume-meta-pt", html)
        self.assertIn("resume-dossier-pt", html)

    def test_live_payload_includes_resume_status_parts(self):
        payload = app.live_payload()
        self.assertIn("resume_status", payload)
        self.assertIn("jobs_hash", payload)
        self.assertIn("copilot_ask", payload)
        self.assertIn("copilot_ask_sound", payload)
        self.assertIn("copilot_ask_sound_volume", payload)
        self.assertIsInstance(payload["copilot_ask_sound"], bool)
        self.assertGreaterEqual(payload["copilot_ask_sound_volume"], 0.0)
        self.assertLessEqual(payload["copilot_ask_sound_volume"], 1.0)
        for lang in ("pt", "en"):
            part = payload["resume_status"][lang]
            self.assertIn("meta_html", part)
            self.assertIn("dossier_html", part)
            self.assertIn("meta_hash", part)
            self.assertIn("dossier_hash", part)
            self.assertNotIn('type="file"', part["meta_html"])
            self.assertNotIn("force_reanalyze", part["meta_html"])

    def test_dossier_hash_stable_when_only_message_changes(self):
        parts_a = app.resume_status_parts("pt")
        # Same analysis content → same dossier hash across calls.
        parts_b = app.resume_status_parts("pt")
        self.assertEqual(parts_a["dossier_hash"], parts_b["dossier_hash"])


class WantsJsonTests(unittest.TestCase):
    def test_accept_json(self):
        self.assertTrue(app.request_wants_json({"Accept": "application/json"}))
        self.assertTrue(app.request_wants_json({"X-Requested-With": "fetch"}))
        self.assertFalse(app.request_wants_json({"Accept": "text/html"}))


if __name__ == "__main__":
    unittest.main()
