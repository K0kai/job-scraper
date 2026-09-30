import unittest

import ats_base
import ats_template


class TemplateSanityTests(unittest.TestCase):
    def test_template_satisfies_interface(self):
        handler = ats_template.MeuAtsHandler()
        self.assertIsInstance(handler, ats_base.BaseATSHandler)

    def test_template_not_registered(self):
        # @register do template é comentado de propósito
        self.assertNotIn(ats_template.MeuAtsHandler, ats_base.HANDLERS)

    def test_default_fill_delegates_to_kernel(self):
        from unittest import mock

        handler = ats_template.MeuAtsHandler()
        ctx = ats_base.ApplyContext(cfg={}, rules=[], resume_path="", cover_letter="")
        with mock.patch("ats_kernel.fill_page") as fill_page:
            fill_page.return_value = ats_base.FillResult(ok=True)
            res = handler.fill(mock.MagicMock(), ctx)
        self.assertTrue(res.ok)
        fill_page.assert_called_once()
        called = fill_page.call_args
        self.assertIs(called.kwargs["handler"], handler)


if __name__ == "__main__":
    unittest.main()
