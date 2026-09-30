import unittest

from app import _notes_html


class NoHandlerRedNoteTests(unittest.TestCase):
    def test_no_handler_gets_red(self):
        html = _notes_html("Vale a pena olhar. NO_HANDLER: apply externo em x.io — nada instalado.")
        self.assertIn("#b3261e", html)
        self.assertIn("NO_HANDLER:", html)

    def test_normal_notes_stay_plain(self):
        html = _notes_html("Vale a pena olhar (score 80). Motivo da IA: bom match.")
        self.assertNotIn("#b3261e", html)

    def test_empty_notes(self):
        self.assertEqual(_notes_html(""), '<p class="hint"></p>')

    def test_html_escaped(self):
        html = _notes_html("NO_HANDLER: <script>alert(1)</script>")
        self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main()
