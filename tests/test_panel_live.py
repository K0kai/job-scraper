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

    def test_live_payload_includes_resume_status(self):
        payload = app.live_payload()
        self.assertIn("resume_status_html", payload)
        self.assertIn("pt", payload["resume_status_html"])
        self.assertIn("en", payload["resume_status_html"])
        for lang in ("pt", "en"):
            self.assertNotIn('type="file"', payload["resume_status_html"][lang])


class WantsJsonTests(unittest.TestCase):
    def test_accept_json(self):
        self.assertTrue(app.request_wants_json({"Accept": "application/json"}))
        self.assertTrue(app.request_wants_json({"X-Requested-With": "fetch"}))
        self.assertFalse(app.request_wants_json({"Accept": "text/html"}))


if __name__ == "__main__":
    unittest.main()
