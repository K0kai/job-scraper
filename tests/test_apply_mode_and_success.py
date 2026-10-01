"""Anti falso-positivo de sucesso + apply_mode."""
from __future__ import annotations

import unittest
from unittest import mock

import ats_inhire
import wait_human
from apply_mode import APPLY_MODE_AUTO, APPLY_MODE_REVIEW, apply_mode_is_auto, normalize_apply_mode
from linkedin_apply import SUCCESS_RE as LI_SUCCESS


class SuccessRegexStrictTests(unittest.TestCase):
    def test_inhire_rejects_continue_registration_and_bare_sucesso(self) -> None:
        re_ = ats_inhire.SUCCESS_RE
        self.assertIsNone(re_.search("Continue registration"))
        self.assertIsNone(re_.search("Com sucesso no processo seletivo em aberto"))
        self.assertIsNotNone(re_.search("Obrigado por sua candidatura"))
        self.assertIsNotNone(re_.search("Recebemos sua candidatura"))

    def test_default_rejects_bare_obrigado(self) -> None:
        re_ = wait_human.DEFAULT_SUCCESS_RE
        self.assertIsNone(re_.search("Obrigado por visitar nosso site"))
        self.assertIsNotNone(re_.search("Obrigado por sua candidatura"))
        self.assertIsNotNone(re_.search("Thank you for applying"))

    def test_linkedin_rejects_bare_submitted(self) -> None:
        self.assertIsNone(LI_SUCCESS.search("Not yet submitted"))
        self.assertIsNone(LI_SUCCESS.search("Submit application"))
        self.assertIsNotNone(LI_SUCCESS.search("Your application was sent"))
        self.assertIsNotNone(LI_SUCCESS.search("Application submitted"))


class ApplyModeTests(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(normalize_apply_mode("auto"), APPLY_MODE_AUTO)
        self.assertEqual(normalize_apply_mode("review"), APPLY_MODE_REVIEW)
        self.assertEqual(normalize_apply_mode(""), APPLY_MODE_REVIEW)
        self.assertEqual(normalize_apply_mode("weird"), APPLY_MODE_REVIEW)

    def test_is_auto(self) -> None:
        self.assertTrue(apply_mode_is_auto({"apply_mode": "auto"}))
        self.assertFalse(apply_mode_is_auto({"apply_mode": "review"}))
        self.assertFalse(apply_mode_is_auto({}))


class WaitSuccessSignalTests(unittest.TestCase):
    def test_wait_success_true_then_false(self) -> None:
        page = mock.MagicMock()
        page.is_closed.return_value = False
        page.inner_text.return_value = "Thank you for applying to our role"
        with mock.patch("wait_human._sleep"), mock.patch("wait_human._now", side_effect=[0, 1, 2]):
            self.assertTrue(wait_human.wait_for_success_signal(page, timeout_s=5, poll_s=0.1))
        page.inner_text.return_value = "Please continue registration"
        with mock.patch("wait_human._sleep"), mock.patch(
            "wait_human._now", side_effect=[0, 1, 2, 3, 100]
        ):
            self.assertFalse(
                wait_human.wait_for_success_signal(
                    page, timeout_s=2, poll_s=0.1, success_regex=ats_inhire.SUCCESS_RE
                )
            )


class RouterApplyModeTests(unittest.TestCase):
    def test_review_mode_does_not_allow_submit_on_no_handler(self) -> None:
        import ats_router

        page = mock.MagicMock()
        page.url = "https://unknown-ats.example/apply"
        page.is_closed.return_value = False
        context = mock.MagicMock()
        context.pages = [page]

        with mock.patch.object(ats_router, "find_handler", return_value=None), mock.patch.object(
            ats_router, "copilot_rescue", return_value=("solved", "ok", page)
        ) as rescue, mock.patch.object(ats_router, "wait_for_human", return_value="timeout"):
            outcome, _detail = ats_router.run_ats_flow(
                page, context, cfg={"apply_mode": "review"}, rules=[], resume_path="", cover_letter=""
            )
        self.assertEqual(outcome, ats_router.OUTCOME_TIMEOUT)
        ai_arg = rescue.call_args.args[3]
        self.assertFalse(ai_arg.get("allow_submit"))

    def test_auto_mode_allows_submit_on_no_handler(self) -> None:
        import ats_router

        page = mock.MagicMock()
        page.url = "https://unknown-ats.example/apply"
        page.is_closed.return_value = False
        context = mock.MagicMock()
        context.pages = [page]

        with mock.patch.object(ats_router, "find_handler", return_value=None), mock.patch.object(
            ats_router, "copilot_rescue", return_value=("submitted", "enviou", page)
        ) as rescue:
            outcome, _detail = ats_router.run_ats_flow(
                page, context, cfg={"apply_mode": "auto"}, rules=[], resume_path="", cover_letter=""
            )
        self.assertEqual(outcome, ats_router.OUTCOME_SUBMITTED)
        self.assertTrue(rescue.call_args.args[3].get("allow_submit"))


if __name__ == "__main__":
    unittest.main()
