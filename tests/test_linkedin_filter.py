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
        # Toda URL tem teto de recência; prioridade ao mesmo dia.
        self.assertTrue(all("f_TPR=" in u for u in urls))
        self.assertTrue(any(f"f_TPR={app.LINKEDIN_TPR_DAY}" in u for u in urls))
        self.assertTrue(any(f"f_TPR={app.LINKEDIN_TPR_WEEK}" in u for u in urls))
        # Mesmo dia vem antes na lista (maior prioridade no scrape).
        first_day = next(i for i, u in enumerate(urls) if f"f_TPR={app.LINKEDIN_TPR_DAY}" in u)
        first_week = next(i for i, u in enumerate(urls) if f"f_TPR={app.LINKEDIN_TPR_WEEK}" in u)
        self.assertLess(first_day, first_week)

    def test_linkedin_age_parser_and_filter(self):
        self.assertEqual(app._linkedin_posted_age_days("Just now"), 0.0)
        self.assertEqual(app._linkedin_posted_age_days("2 hours ago"), 2 / 24)
        self.assertEqual(app._linkedin_posted_age_days("3 days ago"), 3.0)
        self.assertEqual(app._linkedin_posted_age_days("2 weeks ago"), 14.0)
        self.assertTrue(app._linkedin_within_max_age("6 days ago"))
        self.assertFalse(app._linkedin_within_max_age("8 days ago"))
        self.assertTrue(app._linkedin_within_max_age(""))  # desconhecido: não descarta

    def test_normalize_drops_old_linkedin_and_sorts_recent_first(self):
        items = [
            {"title": "Old", "link": "https://x/old", "postedAt": "2 weeks ago"},
            {"title": "Week", "link": "https://x/week", "postedAt": "5 days ago"},
            {"title": "Today", "link": "https://x/today", "postedAt": "2 hours ago"},
        ]
        out = app.normalize_apify_items(items, label="LinkedIn Jobs")
        titles = [j["title"] for j in out]
        self.assertNotIn("Old", titles)
        self.assertEqual(titles[0], "Today")
        self.assertEqual(titles[1], "Week")

    def test_fallback_filter_shape(self):
        fb = app._fallback_linkedin_filter(app.settings())
        self.assertIn("keywords", fb)
        self.assertIn("experience_levels", fb)
        self.assertEqual(fb["source"], "fallback")


if __name__ == "__main__":
    unittest.main()
