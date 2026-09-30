import unittest

import ats_base
import ats_greenhouse
import ats_gupy
import ats_lever


class GreenhouseTests(unittest.TestCase):
    def test_can_handle_boards(self):
        self.assertTrue(
            ats_greenhouse.GreenhouseHandler.can_handle(
                "https://boards.greenhouse.io/acme/jobs/1234567"
            )
        )

    def test_can_handle_job_boards(self):
        self.assertTrue(
            ats_greenhouse.GreenhouseHandler.can_handle(
                "https://job-boards.greenhouse.io/acme/jobs/777"
            )
        )

    def test_rejects_others(self):
        self.assertFalse(
            ats_greenhouse.GreenhouseHandler.can_handle("https://lev-cdn.net/j")
        )

    def test_assisted_first(self):
        self.assertFalse(ats_greenhouse.GreenhouseHandler.auto_submit_capable)

    def test_registered(self):
        self.assertIn(ats_greenhouse.GreenhouseHandler, ats_base.HANDLERS)


class LeverTests(unittest.TestCase):
    def test_can_handle_jobs(self):
        self.assertTrue(
            ats_lever.LeverHandler.can_handle("https://jobs.lever.co/acme/abc-123")
        )

    def test_can_handle_apply_subdomain(self):
        # apply.candidatos.com.br serve o form do Lever — legado comum no BR
        self.assertTrue(
            ats_lever.LeverHandler.can_handle("https://apply.lever.co/acme/abc")
        )

    def test_rejects_greenhouse(self):
        self.assertFalse(
            ats_lever.LeverHandler.can_handle("https://boards.greenhouse.io/x")
        )

    def test_assisted_first(self):
        self.assertFalse(ats_lever.LeverHandler.auto_submit_capable)

    def test_registered(self):
        self.assertIn(ats_lever.LeverHandler, ats_base.HANDLERS)


class GupyTests(unittest.TestCase):
    def test_can_handle_portal(self):
        self.assertTrue(
            ats_gupy.GupyHandler.can_handle("https://portal.gupy.io/job-application/xyz")
        )

    def test_can_handle_wildcard_subdomain(self):
        # evidência: vagas.gruporandonstads.com.br etc. usam *.gupy.io
        self.assertTrue(
            ats_gupy.GupyHandler.can_handle("https://acme.gupy.io/jobs/1234")
        )

    def test_rejects_others(self):
        self.assertFalse(
            ats_gupy.GupyHandler.can_handle("https://inhire.app/x")
        )

    def test_assisted_first(self):
        self.assertFalse(ats_gupy.GupyHandler.auto_submit_capable)

    def test_registered(self):
        self.assertIn(ats_gupy.GupyHandler, ats_base.HANDLERS)


class NoCollisionTests(unittest.TestCase):
    def test_each_url_picks_single_handler(self):
        self.assertIs(
            ats_base.find_handler("https://boards.greenhouse.io/a/j"),
            ats_greenhouse.GreenhouseHandler,
        )
        self.assertIs(
            ats_base.find_handler("https://jobs.lever.co/a/j"),
            ats_lever.LeverHandler,
        )
        self.assertIs(
            ats_base.find_handler("https://x.gupy.io/j"),
            ats_gupy.GupyHandler,
        )


class DetectObstaclesTests(unittest.TestCase):
    """Sites que costumam pedir login/captcha devem declarar o obstáculo."""

    def test_gupy_login_wall_detected(self):
        from unittest import mock

        page = mock.MagicMock()
        page.content.return_value = (
            '<div class="login-required">Entre com sua conta Gupy</div>'
        )
        obstacles = ats_gupy.GupyHandler().detect_obstacles(page)
        self.assertIn("login", obstacles)

    def test_greenhouse_clean_page_no_obstacles(self):
        from unittest import mock

        page = mock.MagicMock()
        page.content.return_value = '<form><input name="name"/></form>'
        self.assertEqual(ats_greenhouse.GreenhouseHandler().detect_obstacles(page), [])


if __name__ == "__main__":
    unittest.main()
