"""Perguntas humanas do copiloto via painel."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dbutil import open_connection

import copilot_asks
from ats_copilot import ACTION_VOCAB, build_copilot_prompt


class CopilotAsksDbTests(unittest.TestCase):
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

    def test_create_answer_appends_facts(self) -> None:
        ask_id = copilot_asks.create_ask(
            self._connect, job_id=196, question="Qual seu GPA?", now_iso="2026-10-01T12:00:00+00:00"
        )
        pending = copilot_asks.get_pending_ask(self._connect)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["id"], ask_id)
        self.assertEqual(pending["question"], "Qual seu GPA?")

        ok = copilot_asks.answer_ask(
            self._connect, ask_id, "3.8 / 4.0", now_iso="2026-10-01T12:01:00+00:00"
        )
        self.assertTrue(ok)
        self.assertIsNone(copilot_asks.get_pending_ask(self._connect))
        facts = copilot_asks.load_facts(self._connect, "pt")
        self.assertIn("Qual seu GPA?: 3.8 / 4.0", facts)
        self.assertIn("GPA", copilot_asks.load_facts(self._connect, "en"))

    def test_wait_returns_on_answer(self) -> None:
        ask_id = copilot_asks.create_ask(
            self._connect, job_id=1, question="University?", now_iso="t0"
        )
        ticks = {"n": 0}

        def fake_sleep(_s):
            ticks["n"] += 1
            if ticks["n"] == 1:
                copilot_asks.answer_ask(self._connect, ask_id, "UFMG", now_iso="t1")

        clock = {"t": 0.0}

        def mono():
            return clock["t"]

        def advance_sleep(s):
            clock["t"] += s
            fake_sleep(s)

        status, answer = copilot_asks.wait_for_answer(
            self._connect,
            ask_id,
            minutes=1,
            poll_s=0.5,
            sleep_fn=advance_sleep,
            monotonic_fn=mono,
        )
        self.assertEqual(status, copilot_asks.STATUS_ANSWERED)
        self.assertEqual(answer, "UFMG")

    def test_cancel_then_wait(self) -> None:
        ask_id = copilot_asks.create_ask(
            self._connect, job_id=1, question="Visa?", now_iso="t0"
        )
        self.assertTrue(copilot_asks.cancel_ask(self._connect, ask_id))
        status, answer = copilot_asks.wait_for_answer(
            self._connect, ask_id, minutes=1, poll_s=0.1,
            sleep_fn=lambda _s: None, monotonic_fn=lambda: 0.0,
        )
        # already cancelled on first poll
        self.assertEqual(status, copilot_asks.STATUS_CANCELLED)
        self.assertEqual(answer, "")


class CopilotAskActionVocabTests(unittest.TestCase):
    def test_vocab_mentions_ask(self) -> None:
        self.assertIn("ask", ACTION_VOCAB)
        prompt = build_copilot_prompt(
            reason="x", facts="", resume_summary="", resume_json="",
            job={}, url="", fields=[], buttons=[], page_text="", history=[],
        )
        self.assertIn("ask", prompt)


if __name__ == "__main__":
    unittest.main()
