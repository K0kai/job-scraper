import unittest

import app


class NormalizeApifyItemsTests(unittest.TestCase):
    def test_indeed_original_apply_url(self):
        items = [
            {
                "id": "abc",
                "title": "Backend Engineer",
                "originalApplyUrl": "https://company.example/jobs/1",
                "viewJobLink": "/viewjob?jk=abc",
                "companyDetails": {"name": "Acme"},
                "formattedLocation": "São Paulo, SP",
                "jobDescription": "Python and APIs",
                "pubDate": 1_700_000_000_000,
            }
        ]
        out = app.normalize_apify_items(items, label="Indeed Jobs")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://company.example/jobs/1")
        self.assertEqual(out[0]["company"], "Acme")
        self.assertEqual(out[0]["location"], "São Paulo, SP")
        self.assertIn("2023", str(out[0]["posted_at"]))

    def test_indeed_relative_view_job_link(self):
        items = [
            {
                "title": "Dev",
                "viewJobLink": "/viewjob?jk=xyz",
                "companyOverviewLink": "https://br.indeed.com/cmp/Foo",
                "jobDescription": "desc",
                "companyDetails": {"name": "Foo"},
            }
        ]
        out = app.normalize_apify_items(items, label="Indeed")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://br.indeed.com/viewjob?jk=xyz")

    def test_linkedin_style_still_works(self):
        items = [{"title": "SWE", "link": "https://linkedin.com/jobs/view/1", "companyName": "Co"}]
        out = app.normalize_apify_items(items, label="LinkedIn")
        self.assertEqual(out[0]["url"], "https://linkedin.com/jobs/view/1")


if __name__ == "__main__":
    unittest.main()
