"""Copiloto: perfil do painel + extrato priorizado do currículo."""
from __future__ import annotations

import json
import unittest

from ats_copilot import (
    build_copilot_prompt,
    copilot_resume_excerpt,
    panel_profile_block,
)


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


if __name__ == "__main__":
    unittest.main()
