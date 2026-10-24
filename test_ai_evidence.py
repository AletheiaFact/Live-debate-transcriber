"""Offline regression tests: mocked Sheets and LLM, no network or credentials."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

import audit_trace
from ai_client import AIError, Client
from ai_config import Config
from ai_evidence import Jobs, Service, route_team
from classification import permit_verdict
from evidence_search import SCOPES, Search, SearchError, TABS, cell, urls
from schemas import Assessment, ExtractedClaim, Extraction, RecordAssessment, Segment
from server import Handler, Store

MATERNITY = "Em 2009, Flávio foi o único dos 49 deputados da Alerj a votar contra ampliar a licença-maternidade."


def claim(text=MATERNITY, **changes):
    return ExtractedClaim(quote=text, normalized_claim=text, checkable=True, subjects=["Flávio"],
                          topic="Direitos sociais", keywords=changes.pop("keywords", ["licença-maternidade", "2009", "49", "Alerj"]),
                          synonyms=changes.pop("synonyms", ["licença maternidade", "ampliar"]), **changes)


def fixture():
    data = []
    for tab in TABS:
        rows = [list(tab.headers)]
        if tab.sheet_id == 1277645709:
            rows += [["EDITORIA: POLÍTICA"], ["Tópico: licença-maternidade"]]
            rows += [[] for _ in range(175 - len(rows))]
            fields = {"A": "LU106", "E": "L2 • Instituições e outras áreas", "F": "Ataques a Flávio",
                      "G": "Licença-maternidade (2009)", "H": "Declaração do candidato", "L": MATERNITY,
                      "N": "O registro da Alerj confirma a votação de 2009 sobre licença de quatro para seis meses.",
                      "P": "Confiável", "V": "https://example.org/checagem", "X": "https://example.org/alerj"}
            row = [fields.get(chr(65 + i), "") for i in range(len(tab.headers))]
            rows.append(row)
        elif tab.sheet_id == 837967820:
            row = [""] * 25
            for col, value in {"A": "FB001", "E": "F1 • Economia", "F": "Economia", "G": "Inflação",
                               "H": "Tópico antecipado", "L": "A inflação acumulada subiu para 8 por cento no ano de 2025",
                               "N": "Série de preços para consulta futura.", "X": "https://example.org/inflacao"}.items():
                row[ord(col) - 65] = value
            rows.append(row)
        elif tab.sheet_id == 191544813:
            row = [""] * 19
            for col, value in {"A": "N01", "B": "Campo de Flávio", "C": "Fraude na apuração do primeiro turno",
                               "D": "Urnas antigas travaram a contagem de votos", "H": "A pausa afetou a divulgação e não a totalização, conforme nota do TSE.",
                               "I": "Falso", "K": "F3 • Instituições; L2 • Instituições", "P": "https://example.org/tse"}.items():
                row[ord(col) - 65] = value
            rows.append(row)
        else:
            rows += [[] for _ in range(19 - len(rows))]
            row = [""] * 19
            for col, value in {"A": "PF16", "B": "Flávio Bolsonaro", "D": "F1 • Economia", "E": "Contas públicas",
                               "F": "Dívida pública", "G": "Diagnóstico com dado", "I": "A dívida pública cresceu 13 pontos percentuais em relação ao PIB",
                               "J": "A série fiscal do Banco Central registra crescimento de 11,2 pontos no período.",
                               "K": "Exagerado", "Q": "https://example.org/divida", "S": "https://example.org/bc"}.items():
                row[ord(col) - 65] = value
            rows.append(row)
        data.append((tab, f"Aba {tab.sheet_id}", rows))
    return data


class FakeSource:
    def __init__(self):
        self.data, self.calls, self.failure = fixture(), 0, False

    def load(self):
        self.calls += 1
        if self.failure:
            raise SearchError("falha de rede simulada")
        return self.data


def assessment(evidence, verdict=None, relevance="DIRECT_MATCH", compatible=True):
    records = []
    for e in evidence:
        tab = next(t for t in TABS if t.sheet_id == e.sheet_id)
        records.append(RecordAssessment(evidence_key=e.evidence_key, relevance=relevance,
                                       summary="Avaliação simulada do documento indicado.", context_compatible=compatible,
                                       source_quality="RELIABLE", basis_quote=e.fields[tab.headers[ord(tab.secondary) - 65]]))
    return Assessment(records=records, suggested_verdict=verdict,
                      verdict_evidence_keys=[e.evidence_key for e in evidence] if verdict else [],
                      explanation="Fontes e contexto precisam de revisão humana.")


class FakeLLM:
    def __init__(self, claims=None, fail_assessment=0, relevance="DIRECT_MATCH", verdict=None):
        self.claims = claims if claims is not None else [claim()]
        self.calls = 0
        self.fail_assessment, self.relevance, self.verdict = fail_assessment, relevance, verdict

    def structured(self, system, data, schema):
        self.calls += 1
        if schema is Extraction:
            return Extraction(claims=self.claims)
        if self.calls == self.fail_assessment:
            raise AIError("JSON de IA inválido para o schema")
        records = []
        for e in data["records"]:
            records.append(RecordAssessment(evidence_key=e["evidence_key"], relevance=self.relevance,
                                           summary="Registro relacionado.", context_compatible=True, source_quality="UNCERTAIN", basis_quote=""))
        return Assessment(records=records, suggested_verdict=self.verdict,
                          verdict_evidence_keys=[r.evidence_key for r in records], explanation="Requer revisão documental.")


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.source = FakeSource()
        self.clock = [0]
        self.search = Search(Config(), self.source, clock=lambda: self.clock[0])
        self.records, self.state, _ = self.search.snapshot()

    def retrieve(self, c):
        return self.search.search(c, self.records, self.state)

    def test_lu106_cross_subject_row_link_and_all_four_tabs(self):
        results = self.retrieve(claim())
        self.assertEqual(results[0].record_id, "LU106")
        e = results[0]
        self.assertEqual(e.sheet_id, 1277645709)
        self.assertEqual(e.row_number, 176)
        self.assertTrue(e.spreadsheet_row_url.endswith("gid=1277645709&range=A176:Y176"))
        self.assertEqual(e.fields["Quem"], "")
        self.assertEqual(e.team, "L2 • Instituições e outras áreas")
        self.assertEqual(len({e.sheet_id for _, _, e in self.records}), 4)

    def test_retrieval_examples_each_layout(self):
        cases = [("Inflação acumulada subiu 8 por cento em 2025", "FB001", 2, "Y"),
                 ("Fraude na apuração do primeiro turno com urnas antigas", "N01", 2, "S"),
                 ("A dívida pública cresceu 13 pontos percentuais do PIB", "PF16", 20, "S")]
        for text, rid, row, last in cases:
            with self.subTest(rid=rid):
                hits = self.retrieve(claim(text, keywords=[], synonyms=[]))
                self.assertEqual(hits[0].record_id, rid)
                self.assertEqual(hits[0].cell_range, f"A{row}:{last}{row}")

    def test_accents_names_and_metadata_not_searched(self):
        self.assertEqual(self.retrieve(claim("FLAVIO", keywords=["Lula", "Flávio"], synonyms=[])), [])
        self.assertEqual(self.retrieve(claim("Campo de Flávio", keywords=[], synonyms=[])), [])
        self.assertEqual(self.retrieve(claim("LICENCA MATERNIDADE 2009", keywords=[], synonyms=[]))[0].record_id, "LU106")
        self.assertEqual(self.retrieve(claim("Confiável Instituições", keywords=[], synonyms=[])), [])

    def test_secondary_field_and_full_row_sources(self):
        hits = self.retrieve(claim("A pausa afetou a divulgação, não a totalização", keywords=[], synonyms=[]))
        self.assertEqual(hits[0].record_id, "N01")
        self.assertEqual(len(hits[0].fields), 19)
        self.assertIn("https://example.org/tse", hits[0].source_urls)

    def test_schema_change_and_duplicate_id_are_errors(self):
        self.source.data[0][2][0][5] = "Equipe errada"
        with self.assertRaises(SearchError):
            Search(Config(), self.source).snapshot()
        self.source.data = fixture()
        self.source.data[0][2].append(self.source.data[0][2][-1])
        with self.assertRaises(SearchError):
            Search(Config(), self.source).snapshot()

    def test_cache_expiration_stale_retry_and_recovery(self):
        self.search.snapshot()
        self.assertEqual(self.source.calls, 1)
        self.source.failure = True
        self.clock[0] = 301
        records, state, warning = self.search.snapshot()
        self.assertEqual(state, "STALE")
        self.assertTrue(warning)
        self.assertEqual(len(records), 4)
        self.search.snapshot()
        self.assertEqual(self.source.calls, 2)
        self.source.failure = False
        self.assertEqual(self.search.snapshot(refresh=True)[1], "FRESH")

    def test_no_cache_failure_is_error(self):
        self.source.failure = True
        with self.assertRaises(SearchError):
            Search(Config(), self.source).snapshot()

    def test_current_row_resolved_on_refresh(self):
        e = self.retrieve(claim())[0]
        self.source.data[1][2].insert(1, ["NEW HEADING"])
        records, state, _ = self.search.snapshot(refresh=True)
        moved = self.search.search(claim(), records, state)[0]
        self.assertEqual(moved.record_id, e.record_id)
        self.assertEqual(moved.row_number, 177)
        self.assertIn("A177:Y177", moved.spreadsheet_row_url)

    def test_readonly_scope_and_url_filter(self):
        self.assertEqual(SCOPES, ["https://www.googleapis.com/auth/spreadsheets.readonly"])
        self.assertEqual(urls("javascript:alert(1) https://example.org/a; https://user:pass@host/a"), ["https://example.org/a"])


class EditorialTests(unittest.TestCase):
    setUp = RetrievalTests.setUp
    retrieve = RetrievalTests.retrieve
    def test_verdict_requires_direct_context_and_basis(self):
        evidence = self.retrieve(claim())
        result = assessment(evidence, "VERDADEIRO")
        self.assertEqual(permit_verdict(result, evidence), "VERDADEIRO")
        result.records[0].context_compatible = False
        self.assertIsNone(permit_verdict(result, evidence))
        result.records[0].context_compatible = True
        result.records[0].basis_quote = "Confiável"
        self.assertIsNone(permit_verdict(result, evidence))

    def test_anticipated_topic_label_and_stale_never_justify_verdict(self):
        evidence = self.retrieve(claim("A inflação acumulada subiu para 8 por cento em 2025", keywords=[], synonyms=[]))
        result = assessment(evidence, "FALSO")
        self.assertIsNone(permit_verdict(result, evidence))
        evidence = self.retrieve(claim())
        result = assessment(evidence, "VERDADEIRO")
        result.records[0].relevance = "BACKGROUND_ONLY"
        self.assertIsNone(permit_verdict(result, evidence))
        result.records[0].relevance = "DIRECT_MATCH"
        evidence[0].cache_status = "STALE"
        self.assertIsNone(permit_verdict(result, evidence))
        result.suggested_verdict = "INSUFICIENTE"
        self.assertIsNone(permit_verdict(result, evidence))

    def test_team_ambiguity_and_explicit_routing(self):
        evidence = self.retrieve(claim("Fraude na apuração do primeiro turno", keywords=[], synonyms=[]))
        evidence[0].relevance = "DIRECT_MATCH"
        assigned, teams = route_team(claim(), evidence, {})
        self.assertIsNone(assigned)
        self.assertEqual(teams, ["F3", "L2"])
        self.assertEqual(route_team(claim(), [], {"direitos sociais": ["L2"]}), ("L2", ["L2"]))

    def test_plan_factual_diagnostic_can_qualify_but_proposals_cannot(self):
        evidence = self.retrieve(claim("A dívida pública cresceu 13 pontos percentuais do PIB", keywords=[], synonyms=[]))
        result = assessment(evidence, "IMPRECISO")
        self.assertEqual(permit_verdict(result, evidence), "IMPRECISO")
        evidence[0].record_type = "Proposta do plano"
        self.assertIsNone(permit_verdict(result, evidence))


class PipelineTests(unittest.TestCase):
    def run_service(self, llm, text=MATERNITY, source=None):
        cfg = Config()
        return Service(cfg, Search(cfg, source or FakeSource()), llm).analyze(
            {"segment_id": "s42", "speaker": "Lula", "transcription": text, "start_time": 132.5, "end_time": 148.9})

    def test_no_checkable_claims_skips_sheets(self):
        source = FakeSource()
        r = self.run_service(FakeLLM([]), "Acho ótimo. Vou melhorar tudo.", source)
        self.assertEqual(r["claims"], [])
        self.assertEqual(r["status"], "COMPLETED")
        self.assertEqual(source.calls, 0)

    def test_multiple_claims_partial_failure_and_metadata(self):
        second = "A dívida pública cresceu 13 pontos percentuais do PIB."
        r = self.run_service(FakeLLM([claim(), claim(second, keywords=[], synonyms=[])], fail_assessment=2), MATERNITY + " " + second)
        self.assertEqual(r["status"], "PARTIAL")
        self.assertEqual(len(r["claims"]), 2)
        self.assertTrue(r["claims"][0]["errors"])
        self.assertFalse(r["claims"][1]["errors"])
        self.assertEqual(r["claims"][0]["speaker"], "Lula")
        self.assertEqual(r["claims"][0]["subjects"], ["Flávio"])
        self.assertEqual(r["claims"][1]["claim_id"], "s42_claim_2")
        self.assertEqual(r["claims"][0]["start_time"], 132.5)

    def test_no_match_null_verdict_and_search_failure_distinct(self):
        text = "A produção de abacates em Marte triplicou."
        r = self.run_service(FakeLLM([claim(text, keywords=[], synonyms=[])]), text)
        c = r["claims"][0]
        self.assertEqual(c["retrieval_status"], "NO_MATCH")
        self.assertIsNone(c["suggested_verdict"])
        self.assertEqual(c["evidence"], [])
        source = FakeSource(); source.failure = True
        r = self.run_service(FakeLLM(), source=source)
        self.assertEqual(r["claims"][0]["retrieval_status"], "SEARCH_ERROR")
        self.assertEqual(r["status"], "PARTIAL")

    def test_quote_changed_is_rejected_and_provider_failure_is_error(self):
        r = self.run_service(FakeLLM(), "Uma outra declaração.")
        self.assertEqual(r["status"], "ERROR")
        self.assertFalse(r["claims"])
        llm = Mock(); llm.structured.side_effect = AIError("provedor indisponível")
        r = self.run_service(llm)
        self.assertEqual(r["status"], "ERROR")

    def test_rejected_keyword_match_keeps_record_for_review(self):
        r = self.run_service(FakeLLM(relevance="NOT_RELEVANT", verdict="FALSO"))
        c = r["claims"][0]
        self.assertEqual(c["retrieval_status"], "REVIEW_REQUIRED")
        self.assertIsNone(c["suggested_verdict"])
        self.assertIsNone(c["assigned_team"])
        self.assertEqual(c["evidence"][0]["record_id"], "LU106")

    def test_assessment_cannot_invent_evidence(self):
        llm = FakeLLM()
        original = llm.structured
        def malicious(system, data, schema):
            result = original(system, data, schema)
            if schema is Assessment:
                result.records[0].evidence_key = "invented:LU999"
            return result
        llm.structured = malicious
        r = self.run_service(llm)
        self.assertTrue(r["claims"][0]["errors"])
        self.assertEqual(r["claims"][0]["evidence"][0]["record_id"], "LU106")

    def test_both_candidates_and_national_indicator(self):
        text = "Lula e Flávio citaram que a dívida pública cresceu 13 pontos percentuais do PIB."
        extracted = claim(text, keywords=["dívida pública", "PIB", "13"], synonyms=[])
        extracted.subjects = ["Lula", "Flávio"]
        r = self.run_service(FakeLLM([extracted]), text)
        c = r["claims"][0]
        self.assertEqual(c["subjects"], ["Lula", "Flávio"])
        self.assertEqual(c["evidence"][0]["record_id"], "PF16")

    def test_assessment_cannot_invent_documentary_quote(self):
        llm = FakeLLM()
        original = llm.structured
        def fabricated(system, data, schema):
            result = original(system, data, schema)
            if schema is Assessment:
                result.records[0].basis_quote = "Documento inventado que não consta da base."
            return result
        llm.structured = fabricated
        r = self.run_service(llm)
        c = r["claims"][0]
        self.assertEqual(c["retrieval_status"], "REVIEW_REQUIRED")
        self.assertIsNone(c["suggested_verdict"])
        self.assertTrue(c["errors"])
        self.assertEqual(c["evidence"][0]["basis_quote"], "")

    def test_stale_snapshot_never_reports_clean_no_match(self):
        source = FakeSource(); clock = [0]
        search = Search(Config(), source, clock=lambda: clock[0]); search.snapshot()
        clock[0] = 301; source.failure = True
        text = "A produção de abacates em Marte triplicou."
        llm = FakeLLM([claim(text, keywords=[], synonyms=[])])
        r = Service(Config(), search, llm).analyze({"segment_id":"x", "speaker":"Lula", "transcription":text})
        self.assertEqual(r["claims"][0]["retrieval_status"], "REVIEW_REQUIRED")
        self.assertEqual(r["status"], "PARTIAL")


class ProviderTests(unittest.TestCase):
    def sdk(self, content='{"claims": []}'):
        sdk = Mock()
        sdk.models.list.return_value.data = [SimpleNamespace(id="qwen3.5:4b"), SimpleNamespace(id="gpt-4.1-mini")]
        sdk.chat.completions.create.return_value.choices = [SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=content, refusal=None))]
        return sdk

    def test_provider_switch_and_json_modes(self):
        for provider, mode in [("ollama", "json_schema"), ("ollama", "json_object"), ("openai", "json_schema")]:
            with self.subTest(provider=provider, mode=mode), patch.dict(os.environ, {"AI_EVIDENCE_PROVIDER": provider, "AI_EVIDENCE_JSON_MODE": mode}, clear=True):
                cfg = Config.from_env(); sdk = self.sdk()
                self.assertEqual(Client(cfg, sdk).structured("JSON", {}, Extraction).claims, [])
                self.assertEqual(sdk.chat.completions.create.call_args.kwargs["response_format"]["type"], mode)
                self.assertIn("192.168.0.180" if provider == "ollama" else "api.openai.com", cfg.base_url)

    def test_generation_parameters_match_provider_and_model(self):
        cases = [
            ("openai", "gpt-5.6-luna", 0.0, None),
            ("openai", "gpt-5-mini-2025-08-07", 0.0, None),
            ("openai", "gpt-6-sol", 0.0, None),
            ("openai", "gpt-6-luna", 0.0, None),
            ("openai", "o3-mini", 0.0, None),
            ("openai", "o1", 0.0, None),
            ("openai", "gpt-4.1-mini", 0.0, 0.0),
            ("openai", "gpt-4o-mini", 0.5, 0.5),
            ("openai", "gpt-4.1-mini", None, None),
            ("ollama", "qwen3.5:4b", 0.0, 0.0),
            ("ollama", "gpt-5-local", 0.0, 0.0),
            ("ollama", "qwen3.5:4b", None, None),
        ]
        for provider, model, configured, expected in cases:
            for mode in ("json_schema", "json_object"):
                with self.subTest(provider=provider, model=model, temperature=configured, mode=mode):
                    cfg = Config(provider=provider, model=model, temperature=configured, json_mode=mode)
                    sdk = self.sdk()
                    sdk.models.list.return_value.data = [SimpleNamespace(id=model)]
                    self.assertEqual(Client(cfg, sdk).structured("JSON", {}, Extraction).claims, [])
                    request = sdk.chat.completions.create.call_args.kwargs
                    expected_limit = "max_completion_tokens" if provider == "openai" else "max_tokens"
                    rejected_limit = "max_tokens" if provider == "openai" else "max_completion_tokens"
                    self.assertEqual(request[expected_limit], 6000)
                    self.assertNotIn(rejected_limit, request)
                    if expected is None:
                        self.assertNotIn("temperature", request)
                    else:
                        self.assertEqual(request["temperature"], expected)
                    if provider == "ollama":
                        self.assertEqual(request["reasoning_effort"], "none")
                    else:
                        self.assertNotIn("reasoning_effort", request)

    def test_provider_errors_explain_failure_without_leaking_payloads(self):
        cases = [
            (400, "unsupported_parameter", "max_tokens", "max_tokens"),
            (400, "unsupported_value", "temperature", "temperature"),
            (400, "invalid_json_schema", "response_format", "schema JSON"),
            (400, "unsupported_parameter", "secret-key-in-param", "requisição"),
            (401, "invalid_api_key", None, "OPENAI_API_KEY"),
            (403, None, None, "permissões"),
            (429, None, None, "cota"),
            (500, None, None, "servidor"),
        ]
        for status, code, parameter, expected in cases:
            with self.subTest(status=status, code=code, parameter=parameter):
                error = RuntimeError("secret-key-in-message and private response payload")
                error.status_code, error.code, error.param = status, code, parameter
                error.body = {"message": "secret-key-in-body"}
                sdk = self.sdk()
                sdk.chat.completions.create.side_effect = error
                events = []
                with audit_trace.Trace(events.append).activate():
                    with self.assertRaises(AIError) as raised:
                        Client(Config(), sdk).structured("JSON", {}, Extraction)
                self.assertIn(expected, str(raised.exception))
                self.assertIn(f"HTTP {status}", str(raised.exception))
                self.assertNotIn("secret-key", str(raised.exception))
                serialized = json.dumps(events, ensure_ascii=False)
                self.assertIn(expected, serialized)
                self.assertNotIn("secret-key", serialized)
                self.assertNotIn("private response payload", serialized)

    def test_malformed_json_refusal_truncation_connectivity_missing_model(self):
        for content in ["not JSON", '{"claims":[],"extra":true}', '{"claims":"wrong"}']:
            with self.subTest(content=content), self.assertRaises(AIError):
                Client(Config(), self.sdk(content)).structured("JSON", {}, Extraction)
        sdk = self.sdk(); sdk.models.list.side_effect = OSError("secret credential must not leak")
        with self.assertRaises(AIError) as raised:
            Client(Config(), sdk).check()
        self.assertNotIn("secret", str(raised.exception))
        sdk = self.sdk(); sdk.models.list.return_value.data = []
        with self.assertRaises(AIError): Client(Config(), sdk).check()
        sdk = self.sdk(); sdk.chat.completions.create.return_value.choices[0].finish_reason = "length"
        with self.assertRaises(AIError): Client(Config(), sdk).structured("JSON", {}, Extraction)

    def test_schema_rejects_invalid_verdict_and_times(self):
        with self.assertRaises(ValueError):
            Assessment.model_validate({"records": [], "suggested_verdict": "Enganoso", "verdict_evidence_keys": [], "explanation": "x"})
        with self.assertRaises(ValueError):
            Segment(segment_id="x", speaker="Lula", transcription="x", start_time=5., end_time=2.)


class JobTests(unittest.TestCase):
    def test_http_state_remains_responsive_during_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            release = threading.Event()
            class BlockingService:
                def analyze(self, value, refresh=False):
                    release.wait(3)
                    return {"status": "COMPLETED", "claims": []}
            class TestHandler(Handler):
                pass
            TestHandler.store = Store(directory)
            TestHandler.outbox = SimpleNamespace(state=lambda: {"configured": False})
            TestHandler.extra_outboxes = []
            TestHandler.evidence = Jobs(Config(enabled=True), directory, BlockingService())
            http = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
            thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
            base = f"http://127.0.0.1:{http.server_port}"
            try:
                body = json.dumps({"segment":{"segment_id":"s", "speaker":"Lula", "transcription":"original"}}).encode()
                with urllib.request.urlopen(urllib.request.Request(base + "/api/ai-evidence", data=body, headers={"Content-Type":"application/json"}), timeout=1) as response:
                    self.assertEqual(response.status, 202)
                    job_id = json.load(response)["job_id"]
                start = time.monotonic()
                with urllib.request.urlopen(base + "/api/state", timeout=1) as response:
                    self.assertTrue(json.load(response)["ai_evidence"]["enabled"])
                self.assertLess(time.monotonic() - start, 1)
                with urllib.request.urlopen(base + "/api/ai-evidence/" + job_id, timeout=1) as response:
                    self.assertEqual(json.load(response)["status"], "PROCESSING")
            finally:
                release.set(); http.shutdown(); http.server_close(); thread.join()

    def test_background_work_bounded_concurrency_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            entered, release = threading.Event(), threading.Event()
            class BlockingService:
                def analyze(self, value, refresh=False):
                    entered.set(); release.wait(3)
                    return {"status": "COMPLETED", "claims": []}
            jobs = Jobs(Config(enabled=True), directory, BlockingService())
            value = {"segment_id": "x", "speaker": "Lula", "transcription": "original"}
            start = time.monotonic(); first = jobs.submit(value)
            self.assertLess(time.monotonic() - start, 1)
            self.assertTrue(entered.wait(1))
            second = jobs.submit(value)
            with self.assertRaises(ValueError): jobs.submit(value)
            value["transcription"] = "changed"
            self.assertEqual(jobs.get(first["job_id"])["input"]["transcription"], "original")
            release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and jobs.get(first["job_id"])["status"] == "PROCESSING":
                time.sleep(.01)
            self.assertEqual(jobs.get(first["job_id"])["status"], "COMPLETED")
            # Let daemon workers finish their local history writes before cleanup.
            while time.monotonic() < deadline and not all(os.path.isfile(os.path.join(directory, "ai_evidence", j["job_id"] + ".json")) for j in [first, second]):
                time.sleep(.01)

    def test_endpoint_snapshots_current_edited_block_and_label(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with open(store.blocks_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"id": "a", "text": "original", "status": "ok", "final": True, "start_ms": 1000, "end_ms": 2000}) + "\n")
            store.set_edits({"a": "edited"}); store.set_label("a", "2")
            body = json.dumps({"id": "a", "refresh": True}).encode()
            responses = []; evidence = Mock(); evidence.cfg.enabled = True
            evidence.submit.return_value = {"job_id": "j", "status": "PROCESSING"}
            handler = SimpleNamespace(store=store, evidence=evidence, rfile=io.BytesIO(body),
                                      _json=lambda code, data: responses.append((code, data)))
            Handler._analyze(handler, len(body))
            self.assertEqual(responses[0][0], 202)
            segment = evidence.submit.call_args.args[0]
            self.assertEqual(segment["transcription"], "edited")
            self.assertEqual(segment["speaker"], "Flávio")
            self.assertEqual(segment["start_time"], 1.)
            self.assertTrue(evidence.submit.call_args.kwargs["refresh"])


if __name__ == "__main__":
    unittest.main()
