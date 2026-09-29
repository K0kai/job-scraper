import unittest

from apply_channels import extract_emails
from form_rules import find_rule_for_label, normalize, pick_select_option


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

    def test_normalize(self):
        self.assertEqual(normalize("Currículo"), "curriculo")


class EmailExtractTests(unittest.TestCase):
    def test_extract_skips_noreply(self):
        text = "Apply at jobs@acme.com or noreply@acme.com"
        self.assertEqual(extract_emails(text), ["jobs@acme.com"])


if __name__ == "__main__":
    unittest.main()
