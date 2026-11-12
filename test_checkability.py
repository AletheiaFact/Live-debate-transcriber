"""Jev client and checkability scoring tests; a fake transport, never the network."""
import json
import tempfile
import unittest
from types import SimpleNamespace

import checkability
from checkability import (CONTEXT_BLOCKS, CONTEXT_CHARS, CRITERIA, QUESTION_NAME, RUBRIC_VERSION, Scores,
                          build_state, percentage, score_block, score_pending)
from jev_client import MOCK_MODEL, TYPESAFE_API_KEY_PLACEHOLDER, JevClient, JevError
from server import Store

LOW = [0.94, 0.05, 0.01]
MIDDLE = [0.15, 0.75, 0.10]
WEAK = [0.10, 0.80, 0.10]
HIGH = [0.01, 0.04, 0.95]

CONTESTATION = "Isso que ele falou é mentira, e eu vou resolver."
FIGURE = "No meu governo o desemprego caiu para 7%."
# Middle band: the block contests a specific figure but states no new fact itself.
MIDDLE_BAND = (25, 60)


def reference_jev(state):
    """A well-behaved Jev for the reference case: a figure is level 2; contesting one seen in the context, level 1."""
    if "7%" in state["target_block"]:
        return HIGH
    if "mentira" in state["target_block"] and any("7%" in b["text"] for b in state["previous_blocks"]):
        return MIDDLE
    return LOW


# What a well-behaved Jev returns for the rubric, keyed by target block.
EXPECTED = {
    "Você é um mentiroso e todo mundo sabe": LOW,
    "Meu governo foi melhor que o seu": LOW,
    "Vou criar 1 milhão de empregos": LOW,
    "O desemprego caiu de 11% para 7% entre 2019 e 2022": HIGH,
    "Vou criar 1 milhão de empregos. Em 2022 o desemprego caiu de 11% para 7%.": HIGH,
    "Você é um mentiroso. Meu governo foi melhor e o seu foi um desastre.": LOW,
}


def config(api_key="test-key", backoff=(1, 2, 4)):
    return SimpleNamespace(api_key=api_key, model="jev-1.13.0", base_url="https://jev.test",
                           timeout=5, backoff=list(backoff))


def answer(probabilities, model="jev-1.13.0"):
    body = {"answers": {QUESTION_NAME: {
        "score": sum(i * p for i, p in enumerate(probabilities)),
        "probabilities": {str(i): p for i, p in enumerate(probabilities)},
        "confidence": 0.9,
    }}, "model": model}
    return 200, json.dumps(body)


class FakeJev:
    """Transport that records requests and answers from a list of responses or a function of the state."""

    def __init__(self, responses=None, by_state=None, reply=answer):
        self.responses = list(responses or [])
        self.by_state = by_state
        self.reply = reply  # turns what by_state returns into a response
        self.requests = []

    def __call__(self, url, headers, body, timeout):
        request = json.loads(body)
        self.requests.append((url, headers, request))
        if self.by_state:
            return self.reply(self.by_state(request["state"]))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(transport, **cfg):
    sleeps = []
    return JevClient(config(**cfg), transport=transport, sleep=sleeps.append), sleeps


class JevClientTests(unittest.TestCase):
    def test_request_format_and_parsed_answer(self):
        fake = FakeJev([answer(HIGH)])
        jev, _ = client(fake)
        result = jev.evaluate({"target_block": "x"}, checkability.QUESTIONS)
        url, headers, request = fake.requests[0]
        self.assertEqual(url, "https://jev.test/v1/systemone")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(request["model"], "jev-1.13.0")
        self.assertEqual(request["state"], {"target_block": "x"})
        self.assertEqual(request["questions"][QUESTION_NAME]["type"], "score")
        self.assertEqual(request["questions"][QUESTION_NAME]["criteria"], CRITERIA)
        self.assertEqual(result["answers"][QUESTION_NAME]["probabilities"], HIGH)
        self.assertEqual(result["answers"][QUESTION_NAME]["confidence"], 0.9)

    def test_retries_timeouts_and_5xx_with_backoff(self):
        fake = FakeJev([(503, "busy"), TimeoutError("timed out"), (429, "slow down"), answer(LOW)])
        jev, sleeps = client(fake)
        result = jev.evaluate({}, checkability.QUESTIONS)
        self.assertEqual(result["answers"][QUESTION_NAME]["probabilities"], LOW)
        self.assertEqual(len(fake.requests), 4)
        for delay, base in zip(sleeps, [1, 2, 4]):
            self.assertTrue(0.8 * base <= delay <= 1.2 * base)

    def test_retries_exhausted(self):
        fake = FakeJev([(502, "bad gateway")] * 3)
        jev, sleeps = client(fake, backoff=(1, 1))
        with self.assertRaises(JevError) as e:
            jev.evaluate({}, checkability.QUESTIONS)
        self.assertTrue(e.exception.retryable)
        self.assertEqual((len(fake.requests), len(sleeps)), (3, 2))

    def test_client_errors_fail_without_retry(self):
        for status in (400, 401, 403):
            fake = FakeJev([(status, "denied")])
            jev, sleeps = client(fake)
            with self.assertRaises(JevError) as e:
                jev.evaluate({}, checkability.QUESTIONS)
            self.assertIn(str(status), str(e.exception))
            self.assertFalse(e.exception.retryable)
            self.assertEqual((len(fake.requests), sleeps), (1, []))

    def test_missing_level_or_answer_fails_loudly(self):
        _, body = answer(HIGH)
        incomplete = json.loads(body)
        del incomplete["answers"][QUESTION_NAME]["probabilities"]["2"]
        for response in [(200, json.dumps(incomplete)), (200, json.dumps({"answers": {}})), (200, "not json")]:
            jev, sleeps = client(FakeJev([response]))
            with self.assertRaises(JevError):
                jev.evaluate({}, checkability.QUESTIONS)
            self.assertEqual(sleeps, [])

    def test_placeholder_key_returns_deterministic_mock_without_requests(self):
        fake = FakeJev()
        jev, _ = client(fake, api_key=TYPESAFE_API_KEY_PLACEHOLDER)
        first = jev.evaluate({"target_block": "a"}, checkability.QUESTIONS)
        again = jev.evaluate({"target_block": "a"}, checkability.QUESTIONS)
        self.assertEqual(fake.requests, [])
        self.assertEqual(first, again)
        self.assertEqual(first["model"], MOCK_MODEL)
        probabilities = first["answers"][QUESTION_NAME]["probabilities"]
        self.assertEqual(len(probabilities), len(CRITERIA))
        self.assertAlmostEqual(sum(probabilities), 1)
        others = {tuple(jev.evaluate({"target_block": t}, checkability.QUESTIONS)
                        ["answers"][QUESTION_NAME]["probabilities"]) for t in "bcdefgh"}
        self.assertGreater(len(others), 1)

    def test_empty_key_is_an_error_not_mock(self):
        for key in ("", None):
            fake = FakeJev()
            jev, _ = client(fake, api_key=key)
            with self.assertRaises(JevError):
                jev.evaluate({}, checkability.QUESTIONS)
            self.assertEqual(fake.requests, [])


class CheckabilityTests(unittest.TestCase):
    def test_percentage_formula(self):
        self.assertEqual(percentage([1, 0, 0]), 0)
        self.assertEqual(percentage([0, 1, 0]), 50)
        self.assertEqual(percentage([0, 0, 1]), 100)
        self.assertEqual(percentage([0.2, 0.3, 0.5]), 65)

    def test_reference_examples(self):
        fake = FakeJev(by_state=lambda state: EXPECTED[state["target_block"]])
        jev, _ = client(fake)
        for text, probabilities in EXPECTED.items():
            result = score_block(jev, {"id": "b", "text": text}, [], {"b": "1"})
            if probabilities is LOW:
                self.assertLess(result["percentage"], 10, text)
            else:
                self.assertGreater(result["percentage"], 90, text)
            self.assertEqual(result["probabilities"], probabilities)
            self.assertEqual((result["model"], result["rubric_version"], result["method"]),
                             ("jev-1.13.0", RUBRIC_VERSION, "jev_direct"))

    def test_reference_case_scores_in_the_middle_band_with_context(self):
        state = checkability.check_states()[-1]
        self.assertEqual(state, {
            "target_block": CONTESTATION,
            "speaker": "Lula",
            "previous_blocks": [{"speaker": "Flávio Bolsonaro", "text": FIGURE}],
        })
        jev, _ = client(FakeJev(by_state=reference_jev))
        with_context = checkability.score_state(jev, state)
        alone = checkability.score_state(jev, {**state, "previous_blocks": []})
        self.assertEqual(with_context["probabilities"].index(max(with_context["probabilities"])), 1)
        self.assertTrue(MIDDLE_BAND[0] <= with_context["percentage"] <= MIDDLE_BAND[1], with_context["percentage"])
        self.assertGreater(with_context["percentage"], alone["percentage"])

    def test_context_is_limited_and_keeps_the_end(self):
        previous = [{"id": f"p{i}", "text": f"block {i} " + "palavra " * 100 + "fim"} for i in range(5)]
        state = build_state({"id": "t", "text": "  alvo   com espaços "}, previous, {"p4": "2"})
        self.assertEqual(state["target_block"], "alvo com espaços")
        self.assertIsNone(state["speaker"])
        self.assertEqual(len(state["previous_blocks"]), CONTEXT_BLOCKS)
        self.assertEqual(state["previous_blocks"][-1]["speaker"], "Flávio Bolsonaro")
        for b in state["previous_blocks"]:
            self.assertLessEqual(len(b["text"]), CONTEXT_CHARS + 1)
            self.assertTrue(b["text"].startswith("…") and b["text"].endswith("fim"))


class SessionScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name
        self.store = Store(self.dir)
        for bid, text in [("a", "O desemprego caiu de 11% para 7%"), ("b", "Isso é mentira"),
                          ("c", "Vou resolver"), ("d", "Pergunta do mediador")]:
            self.append(bid, text)
        self.store.set_label("a", "2")
        self.store.set_label("b", "1")
        self.store.set_label("d", "M")
        self.fake = FakeJev(by_state=lambda state: HIGH if "%" in state["target_block"] else LOW)
        self.jev, _ = client(self.fake)

    def append(self, bid, text, final=True, status="ok"):
        with open(self.store.blocks_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(id=bid, text=text, final=final, status=status,
                                   start="2026-10-23T00:01:00-04:00")) + "\n")

    def run_pass(self):
        return score_pending(self.dir, self.jev, Scores(self.dir))

    def test_scores_persist_with_context_and_only_rescore_on_change(self):
        self.append("live", "ainda falando", final=False)
        self.append("gap", "", status="failed")
        self.assertEqual(self.run_pass(), 3)  # the mediator and the live block are skipped
        scores = Scores(self.dir)
        a, c = scores.get("a"), scores.get("c")
        self.assertEqual((a["percentage"], a["method"], a["source_ids"], a["best_source_id"]),
                         (97.0, "jev_direct", ["a"], None))
        self.assertEqual(a["probabilities"], HIGH)
        self.assertEqual(c["state"]["previous_blocks"],
                         [{"speaker": "Flávio Bolsonaro", "text": "O desemprego caiu de 11% para 7%"},
                          {"speaker": "Lula", "text": "Isso é mentira"}])
        self.assertIsNone(scores.get("d"))
        self.assertEqual(self.run_pass(), 0)
        # An edit changes the block and the context of the next one; a label only that block.
        self.store.set_edits({"b": "Isso é mentira, foi 9%"})
        self.assertEqual(self.run_pass(), 2)
        self.store.set_label("c", "1")
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(Scores(self.dir).get("b")["percentage"], 97.0)

    def test_combined_block_takes_the_best_source_without_calling_jev(self):
        self.store.set_edits({"b": "Isso é mentira, foi 9%"})
        self.run_pass()
        calls = len(self.fake.requests)
        gid = self.store.merge_down("b")
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(len(self.fake.requests), calls)
        item = Scores(self.dir).get(gid)
        self.assertEqual((item["method"], item["percentage"], item["best_source_id"], item["source_ids"]),
                         ("max_of_sources", 97.0, "b", ["b", "c"]))  # 97% + 3.5% -> 97%, never the average
        self.assertEqual(item["probabilities"], HIGH)
        self.assertEqual(item["state"]["target_block"], "Isso é mentira, foi 9% Vou resolver")
        self.assertEqual(self.run_pass(), 0)
        self.store.unmerge(gid)
        self.assertEqual(self.run_pass(), 0)  # the sources' scores are still current

    def test_combined_block_is_rerun_when_sources_do_not_cover_its_text(self):
        self.run_pass()
        gid = self.store.merge_down("b")
        self.run_pass()
        self.store.set_edits({gid: "Isso é mentira, o desemprego foi 9% Vou resolver"})
        calls = len(self.fake.requests)
        self.assertEqual(self.run_pass(), 1)
        item = Scores(self.dir).get(gid)
        self.assertEqual((item["method"], item["percentage"], item["best_source_id"]), ("jev_rerun", 97.0, None))
        self.assertEqual(self.fake.requests[calls][2]["state"], item["state"])
        self.assertEqual(item["state"]["previous_blocks"],
                         [{"speaker": "Flávio Bolsonaro", "text": "O desemprego caiu de 11% para 7%"}])

    def test_combined_before_sources_are_scored_is_rerun(self):
        gid = self.store.merge_down("b")
        self.run_pass()
        item = Scores(self.dir).get(gid)
        self.assertEqual((item["method"], item["percentage"]), ("jev_rerun", 3.5))
        self.assertIsNone(Scores(self.dir).get("b"))

    def test_nested_combination_uses_the_original_sources(self):
        self.store.set_label("a", "")
        self.run_pass()
        outer = self.store.merge_down(self.store.merge_down("a"))
        calls = len(self.fake.requests)
        self.run_pass()
        item = Scores(self.dir).get(outer)
        self.assertEqual(len(self.fake.requests), calls)
        self.assertEqual((item["method"], item["source_ids"], item["best_source_id"]),
                         ("max_of_sources", ["a", "b", "c"], "a"))

    def test_combined_block_state_uses_the_same_context_rules(self):
        gid = self.store.merge_down("b")
        with self.store.lock:
            blocks = self.store.current_blocks()
        combined = next(b for b in blocks if b["id"] == gid)
        state = build_state(combined, blocks[:blocks.index(combined)], self.store.labels)
        self.assertEqual(state, {"target_block": "Isso é mentira Vou resolver", "speaker": "Lula",
                                 "previous_blocks": [{"speaker": "Flávio Bolsonaro",
                                                      "text": "O desemprego caiu de 11% para 7%"}]})

    def test_mock_scores_are_replaced_once_a_real_key_is_set(self):
        mock, _ = client(FakeJev(), api_key=TYPESAFE_API_KEY_PLACEHOLDER)
        self.assertEqual(score_pending(self.dir, mock, Scores(self.dir)), 3)
        self.assertEqual(score_pending(self.dir, mock, Scores(self.dir)), 0)
        self.assertEqual(self.run_pass(), 3)
        self.assertEqual({i["model"] for i in Scores(self.dir).items.values()}, {"jev-1.13.0"})

    def test_reference_case_combined_scores_high(self):
        """The operator combines the figure with the block that contests it.

        The page only combines blocks with at most one speaker label, so both
        blocks are left unlabelled here.
        """
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(temp.name)
        with open(store.blocks_path, "w", encoding="utf-8") as f:
            for bid, text in [("fig", FIGURE), ("con", CONTESTATION)]:
                f.write(json.dumps(dict(id=bid, text=text, final=True, status="ok",
                                       start="2026-10-23T00:01:00-04:00")) + "\n")
        fake = FakeJev(by_state=reference_jev)
        jev, _ = client(fake)
        # Combined before the sources were scored: Jev reads the figure and the contestation together.
        gid = store.merge_down("fig")
        score_pending(temp.name, jev, Scores(temp.name))
        item = Scores(temp.name).get(gid)
        self.assertEqual(item["method"], "jev_rerun")
        self.assertEqual(item["state"]["target_block"], f"{FIGURE} {CONTESTATION}")
        self.assertGreater(item["percentage"], 90)
        # Combined after both were scored: the figure's own score wins, with no new call.
        store.unmerge(gid)
        score_pending(temp.name, jev, Scores(temp.name))
        self.assertTrue(MIDDLE_BAND[0] <= Scores(temp.name).get("con")["percentage"] <= MIDDLE_BAND[1])
        calls = len(fake.requests)
        gid = Store(temp.name).merge_down("fig")
        score_pending(temp.name, jev, Scores(temp.name))
        item = Scores(temp.name).get(gid)
        self.assertEqual((item["method"], item["best_source_id"], len(fake.requests)), ("max_of_sources", "fig", calls))
        self.assertGreater(item["percentage"], 90)

    def test_failure_stores_nothing(self):
        jev, _ = client(FakeJev([(401, "bad key")]))
        with self.assertRaises(JevError):
            score_pending(self.dir, jev, Scores(self.dir))
        self.assertEqual(Scores(self.dir).items, {})


if __name__ == "__main__":
    unittest.main()
