import re
import unittest
from unittest import mock

import ats_base
import ats_router


class FakePage:
    def __init__(self, url="https://site.example/job/1", closed=False, body=""):
        self.url = url
        self._closed = closed
        self._body = body

    def is_closed(self):
        return self._closed

    def inner_text(self, selector):
        return self._body

    def bring_to_front(self):
        pass


def make_handler_cls(name="x", hosts=("site.example",), *, capable=False,
                     obstacles=None, fill_result=None, submit_result=None,
                     verify="unknown"):
    cls = type(
        name.title() + "Handler",
        (ats_base.BaseATSHandler,),
        {
            "name": name,
            "hosts": hosts,
            "auto_submit_capable": capable,
            "wait_ready": lambda self, page, timeout_ms=15000: True,
            "detect_obstacles": lambda self, page: list(obstacles or []),
            "fill": lambda self, page, ctx: fill_result
            or ats_base.FillResult(ok=True, filled=["a", "b"]),
            "submit": lambda self, page: submit_result
            or ats_base.SubmitResult(clicked=True, button_label="Submit"),
            "verify_submitted": lambda self, page: verify,
        },
    )
    return cls


CTX = dict(
    cfg={"candidate_name": "Ana"},
    rules=[],
    resume_path="/tmp/cv.pdf",
    cover_letter="",
    salary="",
)


class ReanchorTests(unittest.TestCase):
    def setUp(self):
        ats_base.HANDLERS.clear()

    def tearDown(self):
        ats_base.HANDLERS.clear()

    def test_no_handler_returns_no_handler_outcome(self):
        page = FakePage("https://desconhecido.org/job")
        outcome, detail = ats_router.run_ats_flow(page, None, human_wait=0, **CTX)
        self.assertEqual(outcome, ats_router.OUTCOME_NO_HANDLER)
        self.assertIn("NO_HANDLER:", detail)
        self.assertIn("desconhecido.org", detail)

    def test_reanchor_switches_to_matching_page(self):
        cls = make_handler_cls("target", ("hired.example.com",))
        ats_base.register(cls)
        good = FakePage("https://hired.example.com/apply")
        ctxobj = mock.MagicMock()
        ctxobj.pages = [FakePage("about:blank"), good]
        page = FakePage("https://hired.example.com/apply")
        with mock.patch.object(ats_router, "wait_for_human", return_value="submitted") as wh:
            outcome, detail = ats_router.run_ats_flow(
                page, ctxobj, human_wait=1, **CTX
            )
        self.assertEqual(outcome, ats_router.OUTCOME_ASSISTED)
        wh.assert_called_once()


class AutoSubmitTests(unittest.TestCase):
    def setUp(self):
        ats_base.HANDLERS.clear()

    def tearDown(self):
        ats_base.HANDLERS.clear()

    def test_capable_no_obstacles_submits(self):
        cls = make_handler_cls("fast", ("fast.example.com",), capable=True,
                               verify="submitted")
        ats_base.register(cls)
        page = FakePage("https://fast.example.com/x")
        outcome, detail = ats_router.run_ats_flow(page, None, human_wait=1, **CTX)
        self.assertEqual(outcome, ats_router.OUTCOME_SUBMITTED)

    def test_capable_but_verify_unknown_falls_back_to_assisted(self):
        cls = make_handler_cls("maybe", ("maybe.example.com",), capable=True,
                               verify="unknown")
        ats_base.register(cls)
        page = FakePage("https://maybe.example.com/x")
        ticks = iter([0.0, 5.0, 10.0, 30.0])  # estoura o deadline de 20s
        with mock.patch.object(ats_router, "_verify_now", side_effect=lambda: next(ticks)), \
             mock.patch.object(ats_router, "_verify_sleep"), \
             mock.patch.object(ats_router, "wait_for_human", return_value="timeout") as wh:
            outcome, _ = ats_router.run_ats_flow(page, None, human_wait=1, **CTX)
        self.assertEqual(outcome, ats_router.OUTCOME_TIMEOUT)
        wh.assert_called_once()

    def test_obstacle_forces_assisted_even_if_capable(self):
        cls = make_handler_cls("captchy", ("captchy.example.com",), capable=True,
                               obstacles=["captcha"], verify="submitted")
        ats_base.register(cls)
        page = FakePage("https://captchy.example.com/x")
        with mock.patch.object(ats_router, "wait_for_human", return_value="submitted") as wh:
            outcome, _ = ats_router.run_ats_flow(page, None, human_wait=1, **CTX)
        self.assertEqual(outcome, ats_router.OUTCOME_ASSISTED)
        wh.assert_called_once()

    def test_not_capable_is_assisted(self):
        cls = make_handler_cls("slow", ("slow.example.com",), capable=False)
        ats_base.register(cls)
        page = FakePage("https://slow.example.com/x")
        with mock.patch.object(ats_router, "wait_for_human", return_value="submitted"):
            outcome, _ = ats_router.run_ats_flow(page, None, human_wait=1, **CTX)
        self.assertEqual(outcome, ats_router.OUTCOME_ASSISTED)


class FailureTests(unittest.TestCase):
    def setUp(self):
        ats_base.HANDLERS.clear()

    def tearDown(self):
        ats_base.HANDLERS.clear()

    def test_wait_ready_false_fails(self):
        cls = make_handler_cls("never", ("never.example.com",))
        cls.wait_ready = lambda self, page, timeout_ms=15000: False
        ats_base.register(cls)
        page = FakePage("https://never.example.com/x")
        outcome, detail = ats_router.run_ats_flow(page, None, human_wait=1, **CTX)
        self.assertEqual(outcome, ats_router.OUTCOME_FAILED)
        self.assertIn("nao carregou", detail)

    def test_fill_error_fails(self):
        cls = make_handler_cls(
            "broken", ("broken.example.com",),
            fill_result=ats_base.FillResult(ok=False, missing=["telefone"],
                                            error=None),
        )
        ats_base.register(cls)
        page = FakePage("https://broken.example.com/x")
        with mock.patch.object(ats_router, "wait_for_human", return_value="timeout"):
            outcome, detail = ats_router.run_ats_flow(page, None, human_wait=1, **CTX)
        # preenchimento incompleto ainda vai para humano, mas o detalhe avisa
        self.assertIn(outcome, (ats_router.OUTCOME_TIMEOUT, ats_router.OUTCOME_ASSISTED))
        self.assertIn("telefone", detail)

    def test_closed_page_fails(self):
        cls = make_handler_cls("ghost", ("ghost.example.com",))
        ats_base.register(cls)
        page = FakePage("https://ghost.example.com/x", closed=True)
        outcome, detail = ats_router.run_ats_flow(page, None, human_wait=1, **CTX)
        self.assertEqual(outcome, ats_router.OUTCOME_FAILED)

    def test_submit_error_fails_to_assisted(self):
        cls = make_handler_cls(
            "nosub", ("nosub.example.com",), capable=True,
            submit_result=ats_base.SubmitResult(clicked=False, error="botao nao encontrado"),
        )
        ats_base.register(cls)
        page = FakePage("https://nosub.example.com/x")
        with mock.patch.object(ats_router, "wait_for_human", return_value="submitted"):
            outcome, _ = ats_router.run_ats_flow(page, None, human_wait=1, **CTX)
        self.assertEqual(outcome, ats_router.OUTCOME_ASSISTED)


class WaitHumanTests(unittest.TestCase):
    def test_submitted_on_success_text(self):
        import wait_human

        page = FakePage(body="Obrigado! Recebemos sua candidatura.")
        with mock.patch("wait_human._sleep", side_effect=[None] * 3):
            out = wait_human.wait_for_human(
                page, minutes=1, success_regex=__import__("re").compile("obrigad", re.I)
            )
        self.assertEqual(out, "submitted")

    def test_timeout_when_never_success(self):
        import wait_human

        page = FakePage(body="preencha os campos")
        times = iter([100.0, 200.0, 300.0, 400.0])
        with mock.patch("wait_human._now", side_effect=lambda: next(times)), \
             mock.patch("wait_human._sleep"):
            out = wait_human.wait_for_human(
                page, minutes=1, success_regex=re.compile("zzz", re.I)
            )
        self.assertEqual(out, "timeout")

    def test_abandoned_when_page_closed(self):
        import wait_human

        page = FakePage(closed=True)
        with mock.patch("wait_human._sleep"):
            out = wait_human.wait_for_human(page, minutes=1)
        self.assertEqual(out, "abandoned")


if __name__ == "__main__":
    unittest.main()
