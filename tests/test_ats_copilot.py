"""Copiloto: perfil do painel + extrato priorizado do currículo."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dbutil import open_connection

import copilot_asks
from ats_copilot import (
    build_copilot_prompt,
    call_ai_with_rate_limit_retry,
    copilot_resume_excerpt,
    panel_profile_block,
)
from resume_pipeline import AiUnavailableError


class PanelProfileBlockTests(unittest.TestCase):
    def test_omits_empty_and_includes_city_salary(self) -> None:
        text = panel_profile_block(
            {
                "candidate_name": "Ana",
                "candidate_email": "",
                "candidate_city": "Belo Horizonte",
                "salary_expectation_brl": "12000",
                "salary_expectation_usd": "",
                "candidate_contract_type": "employee",
            }
        )
        self.assertIn("name: Ana", text)
        self.assertIn("city: Belo Horizonte", text)
        self.assertIn("salary_brl: 12000", text)
        self.assertIn("contract_type: employee", text)
        self.assertNotIn("email:", text)
        self.assertNotIn("salary_usd:", text)


class CopilotResumeExcerptTests(unittest.TestCase):
    def test_prioritizes_education_and_location_over_long_skills(self) -> None:
        data = {
            "technical_skills": [f"Skill{i}" for i in range(80)],
            "experience": [
                {
                    "role": f"Role{i}",
                    "employer": f"Co{i}",
                    "responsibilities": ["did stuff " * 40],
                }
                for i in range(10)
            ],
            "education": ["BSc Computer Science — UFMG"],
            "location_notes": "Based in Belo Horizonte, Brazil; open to remote",
            "work_authorization_notes": "Brazilian citizen; no US visa",
            "headline": "Backend engineer",
        }
        raw = json.dumps(data)
        self.assertNotIn("UFMG", raw[:1600])  # regressão: clip cego perde education
        excerpt = copilot_resume_excerpt(raw, "", budget=2000)
        self.assertIn("UFMG", excerpt)
        self.assertIn("Belo Horizonte", excerpt)
        self.assertIn("Brazilian citizen", excerpt)
        self.assertLessEqual(len(excerpt), 2000)

    def test_fallback_to_summary_when_json_invalid(self) -> None:
        excerpt = copilot_resume_excerpt("not-json{{{", "Summary says studied at USP", budget=200)
        self.assertIn("USP", excerpt)


class BuildCopilotPromptTests(unittest.TestCase):
    def test_includes_panel_and_prioritized_excerpt(self) -> None:
        resume = json.dumps(
            {
                "technical_skills": [f"X{i}" for i in range(50)],
                "education": ["MSc AI — Unicamp"],
                "location_notes": "Campinas / remote BR",
            }
        )
        prompt = build_copilot_prompt(
            reason="stuck",
            facts="speaks PT/EN",
            resume_summary="",
            resume_json=resume,
            panel_profile=panel_profile_block(
                {"candidate_city": "Campinas", "salary_expectation_brl": "15000"}
            ),
            job={"title": "Dev", "company": "Acme", "description": "job"},
            url="https://example.com/apply",
            fields=[],
            buttons=[],
            page_text="",
            history=[],
        )
        self.assertIn("=== PANEL PROFILE ===", prompt)
        self.assertIn("city: Campinas", prompt)
        self.assertIn("salary_brl: 15000", prompt)
        self.assertIn("=== RESUME EXCERPT (prioritized) ===", prompt)
        self.assertIn("Unicamp", prompt)
        self.assertIn("Campinas / remote BR", prompt)
        self.assertIn("PANEL PROFILE for city/salary", prompt)


class RateLimitRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "t.db")
        with self._connect() as db:
            db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO settings(key,value) VALUES('candidate_facts_pt','')")
            db.execute("INSERT INTO settings(key,value) VALUES('candidate_facts_en','')")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _connect(self):
        return open_connection(self.db_path)

    def test_retries_429_keeps_history_then_succeeds(self) -> None:
        calls = {"n": 0}
        history = ["turn 0: ask -> answered (UFMG)"]
        sleeps: list[int] = []
        clock = {"t": 0.0}

        def call_fn(**_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise AiUnavailableError("IA indisponivel (HTTP 429).")
            return '{"action":"done","reason":"ok"}'

        ask_id = copilot_asks.create_ask(
            self._connect, job_id=1, question="University?", now_iso="t0"
        )
        copilot_asks.answer_ask(self._connect, ask_id, "UFMG", now_iso="t1")

        text = call_ai_with_rate_limit_retry(
            call_fn=call_fn,
            prompt="p",
            provider="gemini",
            model="m",
            api_key="k",
            history=history,
            turn=1,
            connect_fn=self._connect,
            open_ask_id=ask_id,
            budget_seconds=120,
            sleep_fn=lambda s: sleeps.append(s) or clock.__setitem__("t", clock["t"] + s),
            monotonic_fn=lambda: clock["t"],
            backoff_fn=lambda attempt, rate_limited=False: 5,
        )
        self.assertIn("done", text)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(any("rate-limit, retry" in h for h in history))
        self.assertIn("ask -> answered", history[0])
        panel = copilot_asks.get_pending_ask(self._connect)
        self.assertIsNotNone(panel)
        self.assertIn("Rate limit", panel["hint"] or "")

    def test_auth_error_does_not_retry(self) -> None:
        history: list[str] = []

        def call_fn(**_kwargs):
            raise AiUnavailableError("IA indisponivel (HTTP 401).")

        with self.assertRaises(AiUnavailableError):
            call_ai_with_rate_limit_retry(
                call_fn=call_fn,
                prompt="p",
                provider="gemini",
                model="m",
                api_key="k",
                history=history,
                turn=0,
                budget_seconds=120,
                sleep_fn=lambda _s: None,
                backoff_fn=lambda *_a, **_k: 1,
            )
        self.assertEqual(history, [])


if __name__ == "__main__":
    unittest.main()
