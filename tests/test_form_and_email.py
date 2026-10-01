import unittest

from apply_channels import extract_emails
from form_rules import (
    extract_current_employer,
    find_rule_for_label,
    infer_candidate_country,
    normalize,
    pick_select_option,
)


class FormRulesTests(unittest.TestCase):
    def test_pick_select_option_fuzzy(self):
        options = ["R$ 10.000 - R$ 15.000", "R$ 15.000 - R$ 20.000", "A combinável"]
        self.assertEqual(pick_select_option(options, "15.000 - 20.000"), "R$ 15.000 - R$ 20.000")

    def test_find_rule_for_label(self):
        rules = [
            {"key": "email", "aliases": "email,e-mail", "mode": "text"},
            {"key": "salary", "aliases": "salary,faixa salarial", "mode": "select"},
        ]
        self.assertEqual(find_rule_for_label("Your Email Address", rules)["key"], "email")
        self.assertEqual(find_rule_for_label("Faixa salarial pretendida", rules)["key"], "salary")

    def test_location_and_company_rules(self) -> None:
        from form_rules import DEFAULT_RULES, resolve_rule_value

        by_key = {r["key"]: r for r in DEFAULT_RULES}
        self.assertEqual(
            find_rule_for_label("Current company", DEFAULT_RULES)["key"], "current_company"
        )
        self.assertEqual(
            find_rule_for_label("Country of origin", DEFAULT_RULES)["key"], "country"
        )
        self.assertEqual(find_rule_for_label("State / Province", DEFAULT_RULES)["key"], "state")
        # city rule must not steal "State"
        self.assertNotEqual(find_rule_for_label("State", DEFAULT_RULES)["key"], "city")
        resume = {
            "experience": ["Engineer — Acme Corp — 2020-present"],
            "location_notes": "Based in Belo Horizonte, Brazil",
        }
        self.assertEqual(
            resolve_rule_value(by_key["current_company"], {}, resume_json=resume),
            "Acme Corp",
        )
        self.assertEqual(
            resolve_rule_value(by_key["country"], {"candidate_city": "Belo Horizonte"}, resume_json=resume),
            "Brazil",
        )
        # Sem evidência → vazio (para a IA perguntar), não inventa Brazil
        self.assertEqual(resolve_rule_value(by_key["country"], {}, resume_json={}), "")
        self.assertEqual(extract_current_employer(resume), "Acme Corp")
        self.assertEqual(infer_candidate_country({"candidate_city": "Campinas"}, None), "")

    def test_normalize(self):
        self.assertEqual(normalize("Currículo"), "curriculo")


class EmailExtractTests(unittest.TestCase):
    def test_extract_skips_noreply(self):
        text = "Apply at jobs@acme.com or noreply@acme.com"
        self.assertEqual(extract_emails(text), ["jobs@acme.com"])


if __name__ == "__main__":
    unittest.main()
