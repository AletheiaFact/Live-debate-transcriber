"""Jev theme suggestion tests; a fake transport, never the network."""
import json
import tempfile
import time
import unittest

import checkability
import themes
from jev_client import MOCK_MODEL, TYPESAFE_API_KEY_PLACEHOLDER, JevError
from server import Store
from test_checkability import FakeJev, client
from themes import FAMILIES, NO_THEME, OPTIONS, QUESTION_NAME, THEME_RUBRIC_VERSION, Themes, theme_pending

ECONOMY, SOCIAL, INSTITUTIONS, ENVIRONMENT = list(OPTIONS)[:4]


def probabilities(theme, peak=0.8):
    rest = (1 - peak) / (len(OPTIONS) - 1)
    return {option: peak if option == theme else rest for option in OPTIONS}


def choice(theme, peak=0.8, model="jev-1.13.0"):
    body = {"answers": {QUESTION_NAME: {"choice": theme, "probabilities": probabilities(theme, peak),
                                        "confidence": 0.7}}, "model": model}
    return 200, json.dumps(body)


def keyword_jev(state):
    """A well-behaved Jev for the session tests: a figure is economy, health is social, the rest has no theme."""
    text = state["target_block"]
    return ECONOMY if "%" in text else SOCIAL if "saúde" in text else NO_THEME


def fake(responses=None, by_state=keyword_jev):
    return FakeJev(responses, by_state=None if responses else by_state, reply=choice)


class ChoiceQuestionTests(unittest.TestCase):
    def test_request_format_and_parsed_answer(self):
        jev_fake = fake([choice(INSTITUTIONS)])
        jev, _ = client(jev_fake)
        result = jev.evaluate({"target_block": "x"}, themes.QUESTIONS)
        url, _, request = jev_fake.requests[0]
        self.assertEqual(url, "https://jev.test/v1/systemone")
        question = request["questions"][QUESTION_NAME]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(question["instructions"], themes.INSTRUCTIONS)
        self.assertEqual(list(question["criteria"].items()), list(OPTIONS.items()))  # order kept on the wire
        answer = result["answers"][QUESTION_NAME]
        self.assertEqual(answer["choice"], INSTITUTIONS)
        self.assertEqual(list(answer["probabilities"]), list(OPTIONS))
        self.assertAlmostEqual(answer["probabilities"][INSTITUTIONS], 0.8)
        self.assertEqual(answer["confidence"], 0.7)

    def test_options_and_families(self):
        self.assertEqual(list(OPTIONS), [
            "Economia e finanças públicas", "Saúde, educação e políticas sociais", "Democracia, Justiça e segurança",
            "Meio ambiente, infraestrutura e internacional", "Sem tema"])
        self.assertEqual(FAMILIES, {
            "Economia e finanças públicas": "Economia e políticas sociais",
            "Saúde, educação e políticas sociais": "Economia e políticas sociais",
            "Democracia, Justiça e segurança": "Instituições e outras temáticas",
            "Meio ambiente, infraestrutura e internacional": "Instituições e outras temáticas",
            "Sem tema": None,
        })
        for theme in OPTIONS:
            jev, _ = client(fake([choice(theme)]))
            result = themes.suggest(jev, {"target_block": "x"})
            self.assertEqual((result["theme"], result["family"]), (theme, FAMILIES[theme]))

    def test_missing_option_choice_or_answer_fails_loudly(self):
        _, body = choice(ECONOMY)
        missing_option = json.loads(body)
        del missing_option["answers"][QUESTION_NAME]["probabilities"][NO_THEME]
        no_probabilities = json.loads(body)
        del no_probabilities["answers"][QUESTION_NAME]["probabilities"]
        no_choice = json.loads(body)
        del no_choice["answers"][QUESTION_NAME]["choice"]
        unknown_choice = json.loads(body)
        unknown_choice["answers"][QUESTION_NAME]["choice"] = "Esportes"
        for data in [missing_option, no_probabilities, no_choice, unknown_choice, {"answers": {}}]:
            jev, sleeps = client(fake([(200, json.dumps(data))]))
            with self.assertRaises(JevError):
                jev.evaluate({}, themes.QUESTIONS)
            self.assertEqual(sleeps, [])

    def test_retries_temporary_errors(self):
        jev_fake = fake([(503, "busy"), TimeoutError("timed out"), choice(ECONOMY)])
        jev, sleeps = client(jev_fake)
        self.assertEqual(themes.suggest(jev, {})["theme"], ECONOMY)
        self.assertEqual((len(jev_fake.requests), len(sleeps)), (3, 2))

    def test_unsupported_question_type_fails_before_any_request(self):
        jev_fake = fake([])
        for key in ("test-key", TYPESAFE_API_KEY_PLACEHOLDER):
            jev, _ = client(jev_fake, api_key=key)
            with self.assertRaises(JevError):
                jev.evaluate({}, {"q": {"type": "text", "instructions": "", "criteria": []}})
        self.assertEqual(jev_fake.requests, [])

    def test_mock_choice_is_deterministic_and_complete(self):
        jev_fake = fake([])
        jev, _ = client(jev_fake, api_key=TYPESAFE_API_KEY_PLACEHOLDER)
        first = themes.suggest(jev, {"target_block": "a"})
        self.assertEqual(first, themes.suggest(jev, {"target_block": "a"}))
        self.assertEqual(jev_fake.requests, [])
        self.assertEqual(first["model"], MOCK_MODEL)
        self.assertEqual(list(first["probabilities"]), list(OPTIONS))
        self.assertAlmostEqual(sum(first["probabilities"].values()), 1)
        self.assertEqual(first["theme"], max(first["probabilities"], key=first["probabilities"].get))
        self.assertEqual(first["family"], FAMILIES[first["theme"]])

    def test_empty_key_is_an_error_not_mock(self):
        for key in ("", None):
            jev_fake = fake([])
            jev, _ = client(jev_fake, api_key=key)
            with self.assertRaises(JevError):
                themes.suggest(jev, {})
            self.assertEqual(jev_fake.requests, [])

    def test_check_cases(self):
        self.assertEqual({expected for _, _, expected in themes.CHECK_CASES}, set(OPTIONS))
        states = checkability.check_states(themes.CHECK_CASES)
        short = states[-1]
        self.assertEqual(short, {"target_block": "Não fiz.", "speaker": "Lula", "previous_blocks": [
            {"speaker": "Flávio Bolsonaro", "text": "Você cortou 30% do orçamento da saúde em 2019."}]})


class SessionThemeTests(unittest.TestCase):
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
        self.fake = fake()
        self.jev, _ = client(self.fake)

    def append(self, bid, text, final=True, status="ok", **extra):
        with open(self.store.blocks_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(id=bid, text=text, final=final, status=status,
                                   start="2026-10-23T00:01:00-04:00", **extra)) + "\n")

    def run_pass(self):
        return theme_pending(self.dir, self.jev, Themes(self.dir))

    def calls(self):
        return len(self.fake.requests)

    def test_themes_persist_with_context_and_only_rerun_on_change(self):
        self.append("live", "ainda falando", final=False)
        self.append("gap", "", status="failed")
        self.append("empty", "   ")
        self.assertEqual(self.run_pass(), 3)  # mediator, live, gap and empty blocks are skipped
        stored = Themes(self.dir)
        a = stored.get("a")
        self.assertEqual(list(a), ["block_id", "source_ids", "theme", "family", "probabilities", "confidence", "model",
                                   "theme_rubric_version", "method", "input_hash", "state", "scored_at"])
        self.assertEqual((a["theme"], a["family"], a["method"], a["source_ids"], a["theme_rubric_version"]),
                         (ECONOMY, "Economia e políticas sociais", "jev_direct", ["a"], THEME_RUBRIC_VERSION))
        self.assertEqual(a["probabilities"], probabilities(ECONOMY))
        self.assertEqual((stored.get("b")["theme"], stored.get("b")["family"]), (NO_THEME, None))
        self.assertEqual(stored.get("c")["state"]["previous_blocks"],
                         [{"speaker": "Flávio Bolsonaro", "text": "O desemprego caiu de 11% para 7%"},
                          {"speaker": "Lula", "text": "Isso é mentira"}])
        for bid in ("d", "live", "gap", "empty"):
            self.assertIsNone(stored.get(bid))
        self.assertEqual(self.run_pass(), 0)
        # An edit changes the block and the context of the next one; a label only that block.
        self.store.set_edits({"b": "Isso é mentira, foi 9%"})
        self.assertEqual(self.run_pass(), 2)
        self.store.set_label("c", "1")
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(Themes(self.dir).get("b")["theme"], ECONOMY)

    def test_waits_for_the_block_to_settle(self):
        just_now = time.time()
        self.append("new", "A saúde piorou", transcribed_at=checkability.now_iso())
        pending = checkability.pending(self.dir, Themes(self.dir), "jev-1.13.0", now=just_now,
                                       sources_for=themes.agreeing_sources, digest_for=themes.input_hash)
        self.assertNotIn("new", [block["id"] for block, *_ in pending])
        pending = checkability.pending(self.dir, Themes(self.dir), "jev-1.13.0", now=just_now + 10,
                                       sources_for=themes.agreeing_sources, digest_for=themes.input_hash)
        self.assertIn("new", [block["id"] for block, *_ in pending])

    def test_combined_block_with_the_same_theme_inherits_without_calling_jev(self):
        self.run_pass()
        calls = self.calls()
        gid = self.store.merge_down("b")  # b and c have no theme
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.calls(), calls)
        item = Themes(self.dir).get(gid)
        self.assertEqual((item["method"], item["theme"], item["family"], item["source_ids"]),
                         ("inherited_from_sources", NO_THEME, None, ["b", "c"]))
        self.assertEqual(item["probabilities"], probabilities(NO_THEME))
        self.assertEqual(item["state"]["target_block"], "Isso é mentira Vou resolver")
        self.assertEqual(self.run_pass(), 0)
        self.store.unmerge(gid)
        self.assertEqual(self.run_pass(), 0)  # the sources' themes are still current
        self.assertEqual(self.calls(), calls)

    def test_inherited_probabilities_are_the_mean_and_keep_the_theme_first(self):
        sources = [{"theme": ECONOMY, "family": FAMILIES[ECONOMY], "model": "jev-1.13.0", "confidence": c,
                    "probabilities": probabilities(ECONOMY, peak)} for peak, c in ((0.9, 0.8), (0.5, 0.4))]
        result = themes.inherit(sources)
        self.assertAlmostEqual(result["probabilities"][ECONOMY], 0.7)
        self.assertEqual(max(result["probabilities"], key=result["probabilities"].get), ECONOMY)
        self.assertEqual(result["confidence"], 0.4)

    def test_combined_block_with_different_themes_is_rerun(self):
        self.store.set_edits({"c": "Vou cuidar da saúde"})
        self.run_pass()
        calls = self.calls()
        gid = self.store.merge_down("b")  # no theme + social; the next block is the mediator's
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.calls(), calls + 1)
        item = Themes(self.dir).get(gid)
        self.assertEqual((item["method"], item["theme"]), ("jev_rerun", SOCIAL))
        self.assertEqual(self.fake.requests[calls][2]["state"], item["state"])
        self.assertEqual(item["state"]["target_block"], "Isso é mentira Vou cuidar da saúde")
        self.store.unmerge(gid)
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.calls(), calls + 1)

    def test_edited_combined_block_is_rerun(self):
        self.run_pass()
        gid = self.store.merge_down("b")
        self.run_pass()
        self.store.set_edits({gid: "Isso é mentira, a saúde piorou. Vou resolver"})
        calls = self.calls()
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.calls(), calls + 1)
        item = Themes(self.dir).get(gid)
        self.assertEqual((item["method"], item["theme"], item["family"]),
                         ("jev_rerun", SOCIAL, "Economia e políticas sociais"))
        self.assertEqual(item["state"]["previous_blocks"],
                         [{"speaker": "Flávio Bolsonaro", "text": "O desemprego caiu de 11% para 7%"}])

    def test_combined_before_sources_have_themes_is_rerun(self):
        gid = self.store.merge_down("b")
        self.run_pass()
        self.assertEqual(Themes(self.dir).get(gid)["method"], "jev_rerun")
        self.assertIsNone(Themes(self.dir).get("b"))

    def test_source_theme_from_another_rubric_version_is_rerun(self):
        self.run_pass()
        stored = Themes(self.dir)
        for bid in ("b", "c"):
            stored.append({**stored.get(bid), "theme_rubric_version": THEME_RUBRIC_VERSION - 1})
        gid = self.store.merge_down("b")
        theme_pending(self.dir, self.jev, Themes(self.dir))
        self.assertEqual(Themes(self.dir).get(gid)["method"], "jev_rerun")

    def test_mock_themes_are_replaced_once_a_real_key_is_set(self):
        mock, _ = client(fake([]), api_key=TYPESAFE_API_KEY_PLACEHOLDER)
        self.assertEqual(theme_pending(self.dir, mock, Themes(self.dir)), 3)
        self.assertEqual(theme_pending(self.dir, mock, Themes(self.dir)), 0)
        self.assertEqual(self.run_pass(), 3)
        self.assertEqual({i["model"] for i in Themes(self.dir).items.values()}, {"jev-1.13.0"})

    def test_failure_stores_nothing(self):
        jev, _ = client(fake([(401, "bad key")]))
        with self.assertRaises(JevError):
            theme_pending(self.dir, jev, Themes(self.dir))
        self.assertEqual(Themes(self.dir).items, {})

    def test_themes_and_scores_are_separate_files(self):
        self.run_pass()
        self.assertEqual(checkability.Scores(self.dir).items, {})
        self.assertEqual(len(Themes(self.dir).items), 3)


if __name__ == "__main__":
    unittest.main()
