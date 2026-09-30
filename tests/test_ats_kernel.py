import re
import unittest
from unittest import mock

import ats_base
import ats_kernel


def make_page(evaluate_returns=None):
    """Fake-page: MagicMock cujo evaluate devolve listas (nunca Mock solto)."""
    page = mock.MagicMock()
    returns = list(evaluate_returns or [])

    def _evaluate(script, *args):
        if returns:
            return returns.pop(0)
        return []

    page.evaluate.side_effect = _evaluate
    return page


FIELD_TEXT = {
    "index": 0,
    "key": "name",
    "label": "Full name",
    "tag": "input",
    "type": "text",
    "name": "full_name",
    "id": "full-name",
    "options": [],
}
FIELD_EMAIL = {
    "index": 1,
    "key": "email",
    "label": "Email",
    "tag": "input",
    "type": "email",
    "name": "email",
    "id": "",
    "options": [],
}

CTX = ats_base.ApplyContext(
    cfg={"candidate_name": "Ana Souza", "candidate_email": "ana@x.com"},
    rules=[
        {"key": "full_name", "aliases": "full name,name", "mode": "text", "value_from": "candidate_name", "value": ""},
        {"key": "email", "aliases": "email,e-mail", "mode": "text", "value_from": "candidate_email", "value": ""},
    ],
    resume_path="/tmp/cv.pdf",
    cover_letter="",
)


class CollectFieldsTests(unittest.TestCase):
    def test_marks_fields_and_returns_list(self):
        page = make_page([[FIELD_TEXT, FIELD_EMAIL]])
        fields = ats_kernel.collect_fields(page)
        self.assertEqual(len(fields), 2)
        # marcação data-radar-field acontece no mesmo evaluate (1 round-trip)
        self.assertEqual(page.evaluate.call_count, 1)

    def test_empty_on_error(self):
        page = mock.MagicMock()
        page.evaluate.side_effect = RuntimeError("boom")
        self.assertEqual(ats_kernel.collect_fields(page), [])


class DismissIntrudersTests(unittest.TestCase):
    def test_clicks_only_close_labels(self):
        page = make_page([[]])
        ats_kernel.dismiss_intruders(page)
        script = page.evaluate.call_args_list[0].args[0]
        # lista negra obrigatória: nunca clicar submit/apply/enviar
        for token in ("submit", "apply", "enviar", "candidatar"):
            self.assertIn(token, script.casefold())


class SafeFillTests(unittest.TestCase):
    def test_ok_on_first_try(self):
        page = mock.MagicMock()
        target = page.locator.return_value.first
        self.assertTrue(ats_kernel.safe_fill(page, "sel", "Ana"))
        target.fill.assert_called_once_with("Ana", timeout=5000)
        page.evaluate.assert_not_called()

    def test_recovers_from_detached_element(self):
        page = mock.MagicMock()
        target = page.locator.return_value.first
        target.fill.side_effect = [RuntimeError("detached"), None]
        close_btn = {"index": 0, "text": "Accept", "visible": True, "tag": "button"}
        page.evaluate.side_effect = [[close_btn], None, None]  # dismiss + cleanups
        with mock.patch("ats_kernel._pause"):
            self.assertTrue(ats_kernel.safe_fill(page, "sel", "Ana"))
        self.assertEqual(target.fill.call_count, 2)

    def test_two_failings_goes_false(self):
        page = mock.MagicMock()
        page.locator.return_value.first.fill.side_effect = RuntimeError("detached")
        page.evaluate.side_effect = RuntimeError("popup fechou a pagina")
        with mock.patch("ats_kernel._pause"):
            self.assertFalse(ats_kernel.safe_fill(page, "sel", "Ana"))


class FillPageTests(unittest.TestCase):
    def test_fills_by_rules(self):
        page = make_page([[FIELD_TEXT, FIELD_EMAIL], None, None])
        with mock.patch("ats_kernel._pause"), mock.patch.object(
            ats_kernel, "wait_for_matching_button", return_value=None
        ):
            res = ats_kernel.fill_page(page, CTX)
        self.assertTrue(res.ok)
        self.assertEqual(sorted(res.filled), ["email", "full_name"])
        self.assertEqual(res.missing, [])

    def test_file_mode_uploads_resume(self):
        file_field = {
            "index": 0, "key": "file", "label": "Upload resume", "tag": "input",
            "type": "file", "name": "resume", "id": "", "options": [],
        }
        ctx = ats_base.ApplyContext(
            cfg={}, rules=[{"key": "resume_file", "aliases": "resume", "mode": "file",
                            "value_from": "", "value": ""}],
            resume_path="/tmp/cv.pdf", cover_letter="",
        )
        page = make_page([[file_field], None, None])
        with mock.patch("ats_kernel._pause"), mock.patch.object(
            ats_kernel, "wait_for_matching_button", return_value=None
        ):
            res = ats_kernel.fill_page(page, ctx)
        self.assertTrue(res.ok)
        page.locator.return_value.first.set_input_files.assert_called_once_with("/tmp/cv.pdf", timeout=8000)

    def test_unmatchable_required_field_goes_missing(self):
        mystery = {
            "index": 0, "key": "weird", "label": "Alien quantity", "tag": "input",
            "type": "text", "name": "alien", "id": "", "options": [], "required": True,
        }
        page = make_page([[mystery], None, None])
        with mock.patch("ats_kernel._pause"), mock.patch.object(
            ats_kernel, "wait_for_matching_button", return_value=None
        ):
            res = ats_kernel.fill_page(page, CTX)
        self.assertFalse(res.ok)
        self.assertIn("Alien quantity", res.missing)

    def test_optional_unmatchable_field_is_skipped(self):
        mystery = {
            "index": 0, "key": "weird", "label": "Alien quantity", "tag": "input",
            "type": "text", "name": "alien", "id": "", "options": [], "required": False,
        }
        page = make_page([[mystery], None, None])
        with mock.patch("ats_kernel._pause"), mock.patch.object(
            ats_kernel, "wait_for_matching_button", return_value=None
        ):
            res = ats_kernel.fill_page(page, CTX)
        self.assertTrue(res.ok)
        self.assertEqual(res.missing, [])

    def test_wizard_does_not_advance_when_missing(self):
        """Com campo obrigatório não preenchido, o kernel não clica Next."""
        mystery = {
            "index": 0, "key": "weird", "label": "Alien quantity", "tag": "input",
            "type": "text", "name": "alien", "id": "", "options": [], "required": True,
        }
        page = make_page([[mystery], None, None])
        next_btn = {"index": 0, "text": "Continue", "visible": True, "tag": "button"}
        with mock.patch("ats_kernel._pause"), mock.patch.object(
            ats_kernel, "wait_for_matching_button", return_value=0
        ) as wait, mock.patch.object(ats_kernel, "scan_buttons", return_value=[next_btn]) as scan:
            res = ats_kernel.fill_page(page, CTX)
        self.assertFalse(res.ok)
        wait.assert_not_called()  # nem procurou o Next

    def test_handler_hooks_are_called(self):
        handler = mock.MagicMock()
        handler.pre_fill.return_value = None
        handler.post_fill.return_value = ["pcd"]
        handler.advance_step.return_value = False  # sem wizard custom
        page = make_page([[FIELD_TEXT, FIELD_EMAIL], None, None])
        with mock.patch("ats_kernel._pause"), mock.patch.object(
            ats_kernel, "wait_for_matching_button", return_value=None
        ):
            res = ats_kernel.fill_page(page, CTX, handler=handler)
        handler.pre_fill.assert_called_once()
        self.assertIn("pcd", res.filled)

    def test_page_crash_returns_error_not_exception(self):
        page = mock.MagicMock()
        page.evaluate.side_effect = RuntimeError("pagina fechada")
        with mock.patch("ats_kernel._pause"):
            res = ats_kernel.fill_page(page, CTX)
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.error)


class SubmitButtonTests(unittest.TestCase):
    def test_find_uses_blacklist_for_linkedin_easy(self):
        buttons = [
            {"index": 0, "text": "Submit application", "aria": "", "visible": True, "tag": "button"},
        ]
        with mock.patch.object(ats_kernel, "scan_buttons", return_value=buttons), \
             mock.patch.object(ats_kernel, "wait_for_matching_button") as wait:
            wait.return_value = 0
            wait.side_effect = None
            idx = ats_kernel.find_submit_button(
                mock.MagicMock(), scanned=[dict(b) for b in buttons]
            )
        self.assertEqual(idx, 0)

    def test_click_submit_marks_and_clicks(self):
        buttons = [{"index": 2, "text": "Submit", "aria": "", "visible": True, "tag": "button"}]
        page = mock.MagicMock()
        with mock.patch.object(ats_kernel, "scan_buttons", side_effect=[buttons, []]), \
             mock.patch.object(ats_kernel, "click_scanned_button") as click, \
             mock.patch("ats_kernel._pause"):
            res = ats_kernel.click_submit(page)
        self.assertTrue(res.clicked)
        self.assertEqual(res.button_label, "Submit")
        click.assert_called_once()

    def test_click_submit_absent_returns_error(self):
        page = mock.MagicMock()
        with mock.patch.object(ats_kernel, "scan_buttons", return_value=[]), \
             mock.patch("ats_kernel._pause"):
            res = ats_kernel.click_submit(page)
        self.assertFalse(res.clicked)
        self.assertIsNotNone(res.error)


class PatternListsTests(unittest.TestCase):
    def test_submit_patterns_match_common_labels(self):
        for label in ("Submit application", "Finalizar candidatura", "Enviar candidatura", "Submit"):
            self.assertTrue(any(p.search(label) for p in ats_kernel.SUBMIT_PATTERNS), label)

    def test_next_patterns(self):
        for label in ("Next", "Continue", "Avançar"):
            self.assertTrue(any(p.search(label) for p in ats_kernel.NEXT_PATTERNS), label)


if __name__ == "__main__":
    unittest.main()
