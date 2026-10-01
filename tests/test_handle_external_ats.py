"""_handle_external_ats deve ler a vaga de ai['job'], nao de um parametro job."""
from __future__ import annotations

import unittest
from unittest import mock

from linkedin_apply import _handle_external_ats


class HandleExternalAtsJobContextTests(unittest.TestCase):
    def test_salary_uses_job_from_ai_without_nameerror(self) -> None:
        page = mock.MagicMock()
        page.url = "https://acme.inhire.app/vagas/1"
        context = mock.MagicMock()
        context.pages = [page]
        job = {
            "id": 168,
            "title": "Engenheiro PJ",
            "company": "Acme",
            "description": "Contrato PJ remoto",
            "language": "pt",
        }
        finish_ok = mock.Mock(return_value=(True, "ok"))
        block = mock.Mock(return_value=(False, "blocked"))

        with mock.patch("linkedin_apply._salary_from_rules", return_value="12000") as salary_mock, \
             mock.patch("linkedin_apply.job_context_text", return_value="job-text") as ctx_mock, \
             mock.patch("ats_router.find_handler", return_value=object()), \
             mock.patch("ats_router.run_ats_flow", return_value=("assisted", "preenchido")) as flow_mock:
            result = _handle_external_ats(
                page=page,
                context=context,
                cfg={},
                rules=[],
                resume_path="/tmp/cv.pdf",
                cover_letter="hi",
                human_wait=5,
                finish_ok=finish_ok,
                block=block,
                ai={"job": job},
            )

        self.assertEqual(result, (True, "ok"))
        ctx_mock.assert_called_once_with(job)
        salary_mock.assert_called_once()
        self.assertEqual(salary_mock.call_args.args[2], "job-text")
        self.assertEqual(flow_mock.call_args.kwargs.get("salary"), "12000")
        finish_ok.assert_called_once()
