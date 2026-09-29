import unittest
from unittest import mock

import browser_engine


class ResolveEngineTests(unittest.TestCase):
    def test_default_is_pydoll(self):
        mod = browser_engine.resolve_sync_playwright({"browser_engine": "pydoll"})
        self.assertEqual(mod.__name__, "pydoll.playwright.sync_api")

    def test_playwright_fallback(self):
        mod = browser_engine.resolve_sync_playwright({"browser_engine": "playwright"})
        self.assertEqual(mod.__name__, "playwright.sync_api")

    def test_unknown_engine_falls_back_to_pydoll(self):
        mod = browser_engine.resolve_sync_playwright({"browser_engine": "selenium"})
        self.assertEqual(mod.__name__, "pydoll.playwright.sync_api")

    def test_missing_pydoll_returns_playwright(self):
        with mock.patch.dict("sys.modules", {"pydoll.playwright.sync_api": None}, clear=False):
            mod = browser_engine.resolve_sync_playwright({"browser_engine": "pydoll"})
            self.assertEqual(mod.__name__, "playwright.sync_api")

    def test_engine_name(self):
        self.assertEqual(browser_engine.engine_name({"browser_engine": "pydoll"}), "pydoll")
        self.assertEqual(browser_engine.engine_name({}), "pydoll")

    def test_install_hint_pydoll(self):
        hint = browser_engine.engine_install_hint({"browser_engine": "pydoll"})
        self.assertIn("pip install pydoll-python", hint)

    def test_install_hint_playwright(self):
        hint = browser_engine.engine_install_hint({"browser_engine": "playwright"})
        self.assertIn("playwright install", hint)


class ApplyViaBrowserEngineTests(unittest.TestCase):
    def test_apply_via_browser_module_importable(self):
        import apply_channels

        self.assertTrue(callable(apply_channels.apply_via_browser))


if __name__ == "__main__":
    unittest.main()