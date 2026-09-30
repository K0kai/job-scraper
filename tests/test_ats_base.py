import unittest

import ats_base


class _Alpha(ats_base.BaseATSHandler):
    name = "alpha"
    hosts = ("alpha.example.com",)

    def wait_ready(self, page, timeout_ms: int = 15000) -> bool:
        return True

    def detect_obstacles(self, page) -> list[str]:
        return []


class _Beta(ats_base.BaseATSHandler):
    name = "beta"
    hosts = ("*.beta.example.com", "job-beta.net")

    def wait_ready(self, page, timeout_ms: int = 15000) -> bool:
        return True

    def detect_obstacles(self, page) -> list[str]:
        return []


class ApplyContextTests(unittest.TestCase):
    def test_fields(self):
        ctx = ats_base.ApplyContext(
            cfg={"candidate_name": "Ana"},
            rules=[{"key": "full_name"}],
            resume_path="/tmp/cv.pdf",
            cover_letter="oi",
        )
        self.assertEqual(ctx.cfg["candidate_name"], "Ana")
        self.assertEqual(ctx.salary, "")  # default

    def test_fill_result_defaults(self):
        res = ats_base.FillResult(ok=True, filled=["name"])
        self.assertEqual(res.missing, [])
        self.assertIsNone(res.error)

    def test_submit_result_defaults(self):
        res = ats_base.SubmitResult(clicked=True, button_label="Submit application")
        self.assertIsNone(res.error)


class CanHandleTests(unittest.TestCase):
    def test_exact_host_match(self):
        self.assertTrue(_Alpha.can_handle("https://alpha.example.com/jobs/12"))

    def test_www_tolerated(self):
        self.assertTrue(_Alpha.can_handle("https://www.alpha.example.com/j"))

    def test_port_tolerated(self):
        self.assertTrue(_Alpha.can_handle("http://alpha.example.com:8080/j"))

    def test_non_match(self):
        self.assertFalse(_Alpha.can_handle("https://other.example.com/j"))

    def test_wildcard_subdomain(self):
        self.assertTrue(_Beta.can_handle("https://acme.beta.example.com/v"))

    def test_bad_url_no_crash(self):
        self.assertFalse(_Alpha.can_handle("not a url at all"))
        self.assertFalse(_Alpha.can_handle(""))


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = list(ats_base.HANDLERS)
        ats_base.HANDLERS.clear()

    def tearDown(self):
        ats_base.HANDLERS.clear()
        ats_base.HANDLERS.extend(self._snapshot)

    def test_registration_and_lookup(self):
        ats_base.register(_Alpha)
        self.assertIs(ats_base.find_handler("https://alpha.example.com/x"), _Alpha)
        self.assertIsNone(ats_base.find_handler("https://nothing.example.org/x"))

    def test_duplicate_name_rejected(self):
        ats_base.register(_Alpha)

        class Alpha2(ats_base.BaseATSHandler):
            name = "alpha"  # mesmo nome
            hosts = ("gamma.example.com",)

        with self.assertRaises(RuntimeError):
            ats_base.register(Alpha2)

    def test_duplicate_host_rejected(self):
        ats_base.register(_Alpha)

        class AlphaClone(ats_base.BaseATSHandler):
            name = "alpha-clone"
            hosts = ("alpha.example.com",)  # host já reivindicado

        with self.assertRaises(RuntimeError):
            ats_base.register(AlphaClone)

    def test_lookup_skips_unregistered(self):
        self.assertIsNone(ats_base.find_handler("https://alpha.example.com/x"))


class InterfaceDefaultsTests(unittest.TestCase):
    def test_auto_submit_default_false(self):
        self.assertFalse(_Alpha.auto_submit_capable)

    def test_abstract_requires_core_methods(self):
        with self.assertRaises(TypeError):
            ats_base.BaseATSHandler()  # wait_ready/detect_obstacles são abstratos


if __name__ == "__main__":
    unittest.main()
