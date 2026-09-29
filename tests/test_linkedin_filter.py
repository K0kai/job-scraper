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
                "locations": ["Brazil"],
                "experience_levels": [3, 4],
                "workplace_types": [2],
            },
            max_urls=2,
        )
        self.assertEqual(len(urls), 1)
        self.assertIn("f_E=3%2C4", urls[0])
        self.assertIn("f_WT=2", urls[0])
        self.assertIn("keywords=backend", urls[0])

    def test_fallback_filter_shape(self):
        fb = app._fallback_linkedin_filter(app.settings())
        self.assertIn("keywords", fb)
        self.assertIn("experience_levels", fb)
        self.assertEqual(fb["source"], "fallback")


if __name__ == "__main__":
    unittest.main()
