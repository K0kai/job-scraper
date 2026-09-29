import unittest

import app


class LinkedInFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.initialize()

    def test_normalize_fills_defaults(self):
        cfg = app.settings()
        out = app._normalize_linkedin_filter({"keywords": ["Python"], "experience_levels": [4]}, cfg)
        self.assertEqual(out["keywords"], ["Python"])
        self.assertEqual(out["experience_levels"], [4])
        self.assertTrue(out["locations"])
        self.assertTrue(out["workplace_types"])

    def test_build_urls_include_experience_and_workplace(self):
        urls = app.build_linkedin_search_urls(
            {
                "keywords": ["backend engineer"],
                "locations": ["Remote", "Belo Horizonte"],
                "experience_levels": [3, 4],
                "workplace_types": [2, 3],
            },
            max_urls=4,
        )
        self.assertGreaterEqual(len(urls), 2)
        joined = "\n".join(urls)
        self.assertIn("f_E=3%2C4", joined)
        self.assertIn("f_WT=2", joined)
        self.assertIn("f_WT=3", joined)
        self.assertIn("Belo", joined)
        # Remote URLs should not be Brazil-only.
        self.assertTrue(any("f_WT=2" in u and "Remote" in u for u in urls))

    def test_fallback_filter_shape(self):
        fb = app._fallback_linkedin_filter(app.settings())
        self.assertIn("keywords", fb)
        self.assertIn("experience_levels", fb)
        self.assertEqual(fb["source"], "fallback")


if __name__ == "__main__":
    unittest.main()
