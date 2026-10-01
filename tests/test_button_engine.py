import re
import unittest
from unittest import mock


class ChromeLaunchArgsTests(unittest.TestCase):
    """Launch args determinísticos: stealth + --no-sandbox quando root."""

    def test_root_adds_no_sandbox(self):
        import browser_engine

        with mock.patch("browser_engine._is_root", return_value=True):
            args = browser_engine.chrome_launch_args()
        self.assertIn("--no-sandbox", args)
        self.assertIn("--disable-blink-features=AutomationControlled", args)

    def test_non_root_omits_no_sandbox(self):
        import browser_engine

        with mock.patch("browser_engine._is_root", return_value=False):
            args = browser_engine.chrome_launch_args()
        self.assertNotIn("--no-sandbox", args)

    def test_root_sandbox_extra_override(self):
        import browser_engine

        with mock.patch("browser_engine._is_root", return_value=True):
            args = browser_engine.chrome_launch_args(root_no_sandbox=False)
        self.assertNotIn("--no-sandbox", args)


class PersistentLaunchKwargsTests(unittest.TestCase):
    def test_kwargs_shape(self):
        import browser_engine

        kw = browser_engine.persistent_launch_kwargs(
            profile="/tmp/p",
            headless=False,
            slow_mo=60,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        self.assertEqual(kw["user_data_dir"], "/tmp/p")
        self.assertFalse(kw["headless"])
        self.assertEqual(kw["slow_mo"], 60)
        self.assertEqual(kw["locale"], "en-US")
        self.assertIn("--disable-blink-features=AutomationControlled", kw["args"])
        self.assertIn("--enable-automation", kw["ignore_default_args"])

    def test_extra_args_merged_without_duplicates(self):
        import browser_engine

        kw = browser_engine.persistent_launch_kwargs(
            profile="/tmp/p",
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self.assertEqual(kw["args"].count("--disable-blink-features=AutomationControlled"), 1)
        self.assertIn("--no-sandbox", kw["args"])

    def test_background_run_args_disable_throttling(self):
        import browser_engine

        for arg in (
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion",
        ):
            self.assertIn(arg, browser_engine.BACKGROUND_RUN_ARGS)

        kw = browser_engine.persistent_launch_kwargs(
            profile="/tmp/p",
            headless=False,
            args=list(browser_engine.BACKGROUND_RUN_ARGS),
        )
        for arg in browser_engine.BACKGROUND_RUN_ARGS:
            self.assertIn(arg, kw["args"])


FAKE_BUTTONS = [
    {"index": 0, "text": "Sign in", "aria": "", "visible": True, "tag": "button"},
    {"index": 1, "text": "", "aria": "Easy Apply", "visible": True, "tag": "button"},
    {"index": 2, "text": "Apply on company site", "aria": "", "visible": True, "tag": "a[href]"},
    {"index": 3, "text": "hidden helper", "aria": "", "visible": False, "tag": "button"},
]


class ScanButtonsTests(unittest.TestCase):
    def test_scan_delegates_to_page_evaluate(self):
        import browser_engine

        page = mock.Mock()
        page.evaluate.return_value = FAKE_BUTTONS
        self.assertEqual(browser_engine.scan_buttons(page), FAKE_BUTTONS)
        page.evaluate.assert_called_once()

    def test_scan_empty_on_error(self):
        import browser_engine

        page = mock.Mock()
        page.evaluate.side_effect = RuntimeError("deu ruim")
        self.assertEqual(browser_engine.scan_buttons(page), [])


class FindFirstButtonTests(unittest.TestCase):
    def test_matches_visible_button_by_aria(self):
        import browser_engine

        idx = browser_engine.find_first_button(
            FAKE_BUTTONS, [re.compile(r"easy\s*apply", re.I)]
        )
        self.assertEqual(idx, 1)

    def test_skips_hidden(self):
        import browser_engine

        # Nenhum visível casa; o hidden que casa não deve ser retornado visível.
        idx = browser_engine.find_first_button(
            FAKE_BUTTONS, [re.compile(r"hidden helper", re.I)]
        )
        self.assertIsNone(idx)

    def test_exclude_skips_match(self):
        import browser_engine

        idx = browser_engine.find_first_button(
            FAKE_BUTTONS,
            [re.compile(r"apply", re.I)],
            exclude=[re.compile(r"easy", re.I)],
        )
        self.assertEqual(idx, 2)

    def test_no_match_returns_none(self):
        import browser_engine

        idx = browser_engine.find_first_button(FAKE_BUTTONS, [re.compile(r"nope", re.I)])
        self.assertIsNone(idx)


class WaitForMatchingButtonTests(unittest.TestCase):
    def test_polls_until_match_appears(self):
        import browser_engine

        with mock.patch("browser_engine.scan_buttons", side_effect=[
            [FAKE_BUTTONS[0]],          # 1ª varredura: só "Sign in"
            [FAKE_BUTTONS[0], FAKE_BUTTONS[1]],  # 2ª: apareceu Easy Apply
        ]), mock.patch("browser_engine._poll_sleep"):
            idx = browser_engine.wait_for_matching_button(
                mock.Mock(),
                [re.compile(r"easy\s*apply", re.I)],
                timeout_ms=5000,
                interval_s=0.1,
            )
        self.assertEqual(idx, 1)

    def test_returns_none_on_timeout(self):
        import browser_engine

        with mock.patch("browser_engine.scan_buttons", return_value=[FAKE_BUTTONS[0]]), \
             mock.patch("browser_engine._poll_sleep"):
            idx = browser_engine.wait_for_matching_button(
                mock.Mock(),
                [re.compile(r"easy\s*apply", re.I)],
                timeout_ms=50,
                interval_s=0.1,
            )
        self.assertIsNone(idx)


if __name__ == "__main__":
    unittest.main()