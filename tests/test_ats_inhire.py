import re
import unittest
from unittest import mock

import ats_base
import ats_inhire


class CanHandleTests(unittest.TestCase):
    def test_subdomain(self):
        self.assertTrue(ats_inhire.InHireHandler.can_handle("https://acme.inhire.app/vagas/1"))

    def test_root_domain(self):
        self.assertTrue(ats_inhire.InHireHandler.can_handle("https://inhire.app/x"))

    def test_www(self):
        self.assertTrue(ats_inhire.InHireHandler.can_handle("https://www.inhire.app/x"))

    def test_legacy_substring_weird_url(self):
        self.assertTrue(ats_inhire.InHireHandler.can_handle("inhire.app/jobs"))

    def test_foreign_host(self):
        self.assertFalse(ats_inhire.InHireHandler.can_handle("https://greenhouse.io/x"))


class RegistryTests(unittest.TestCase):
    def test_registered(self):
        self.assertIn(ats_inhire.InHireHandler, ats_base.HANDLERS)
        self.assertIs(
            ats_base.find_handler("https://x.inhire.app/v"), ats_inhire.InHireHandler
        )

    def test_assisted_first(self):
        self.assertFalse(ats_inhire.InHireHandler.auto_submit_capable)


class LegacyHelpersTests(unittest.TestCase):
    """Comportamento que o painel já dependia precisa continuar via handler."""

    def test_can_handle_replaces_legacy_url_check(self):
        self.assertTrue(ats_inhire.InHireHandler.can_handle("https://a.inhire.app/j"))
        self.assertFalse(ats_inhire.InHireHandler.can_handle("https://a.example.com/j"))

    def test_linkedin_profile_value(self):
        self.assertEqual(
            ats_inhire._linkedin_profile_value({"candidate_linkedin": "ana-souza"}),
            "https://www.linkedin.com/in/ana-souza",
        )
        self.assertEqual(
            ats_inhire._linkedin_profile_value({"candidate_linkedin": "https://linkedin.com/in/ana"}),
            "https://linkedin.com/in/ana",
        )

    def test_local_phone_strips_dial_code(self):
        cfg = {"candidate_phone": "+55 (31) 91234-5678"}
        self.assertEqual(ats_inhire._local_phone_digits(cfg), "31912345678")

    def test_local_phone_short_untouched(self):
        cfg = {"candidate_phone": "31912345678"}
        self.assertEqual(ats_inhire._local_phone_digits(cfg), "31912345678")


class FillOutcomeTests(unittest.TestCase):
    """A ordem telefone (código antes do número) é invariante histórico."""

    def test_phone_code_selected_before_number(self):
        handler = ats_inhire.InHireHandler()
        calls: list[str] = []
        page = mock.MagicMock()
        page.locator.side_effect = lambda sel: calls.append(f"locator:{sel}") or mock.MagicMock()
        page.content.return_value = "<html>sem captcha</html>"
        with mock.patch.object(handler, "wait_ready", return_value=True), \
             mock.patch("ats_inhire._select_phone_country_code",
                        side_effect=lambda p, dial: calls.append("phone_code") or True), \
             mock.patch("ats_inhire._select_country_brazil", return_value=True), \
             mock.patch("ats_inhire._select_react_dropdown", return_value=True), \
             mock.patch("ats_inhire._click_visible_choice", return_value=True), \
             mock.patch("ats_inhire._fill_diversity_step"), \
             mock.patch("ats_inhire._prefer_english_ui"), \
             mock.patch("ats_inhire._continue_disabled", return_value=False), \
             mock.patch("ats_inhire._pause"):
            ctx = ats_base.ApplyContext(
                cfg={"candidate_name": "Ana", "candidate_email": "a@x.com",
                     "candidate_phone": "+55 31 91234-5678"},
                rules=[], resume_path="", cover_letter="",
            )
            res = handler.fill(page, ctx)
        self.assertTrue(res.ok)
        order = [c for c in calls if c in ("phone_code",) or c.startswith("locator:#phone")]
        self.assertEqual(order[0], "phone_code")

    def test_detect_obstacles_captcha(self):
        page = mock.MagicMock()
        page.content.return_value = "<div class='g-recaptcha'></div>"
        self.assertEqual(ats_inhire.InHireHandler().detect_obstacles(page), ["captcha"])

    def test_wait_ready_true_when_field_visible(self):
        page = mock.MagicMock()
        self.assertTrue(ats_inhire.InHireHandler().wait_ready(page))

    def test_wait_ready_false_when_absent(self):
        page = mock.MagicMock()
        page.locator.return_value.first.wait_for.side_effect = RuntimeError("timeout")
        self.assertFalse(ats_inhire.InHireHandler().wait_ready(page, timeout_ms=1))


class SuccessRegexTests(unittest.TestCase):
    def test_matches_thank_you(self):
        self.assertTrue(ats_inhire.SUCCESS_RE.search("Obrigado! Recebemos sua candidatura."))

    def test_regex_compiled(self):
        self.assertIsInstance(ats_inhire.InHireHandler.success_regex, re.Pattern)


if __name__ == "__main__":
    unittest.main()
