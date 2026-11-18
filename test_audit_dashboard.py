"""Offline audit integration tests: real pipeline and HTTP, mocked inference/Sheets."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import audit_trace
import test_ai_evidence as fixtures
from ai_client import Client
from ai_config import Config
from ai_evidence import Jobs, Service
from evidence_search import Search
from server import Handler, Store


def simulated_client(cfg, mode="valid", barrier=None):
    sdk = Mock()
    sdk.models.list.return_value.data = [SimpleNamespace(id=cfg.model)]

    def complete(**request):
        data = json.loads(request["messages"][1]["content"])
        if "records" not in data:
            if barrier:
                barrier.wait(3)
            text = data["transcription"]
            c = fixtures.claim(text, keywords=[], synonyms=[]).model_dump()
            if mode == "changed_quote":
                c["quote"] = "Citação inventada pela IA."
            content = json.dumps({"claims": [] if mode == "no_claims" else [c]})
            if mode == "invalid_json":
                content = "this is not JSON"
            if mode == "timeout":
                raise type("APITimeoutError", (Exception,), {})("secret transport payload")
        else:
            records = [{"evidence_key": r["evidence_key"], "relevance": "DIRECT_MATCH", "summary": "Avaliação de teste.",
                        "context_compatible": True, "source_quality": "RELIABLE",
                        "basis_quote": r["fields"].get("O que os dados mostram", "")} for r in data["records"]]
            if mode == "invented_id":
                records[0]["evidence_key"] = "invented:XX999"
            if mode == "invented_basis":
                records[0]["basis_quote"] = "Um trecho de documento que não existe na planilha."
            content = json.dumps({"records": records, "suggested_verdict": "VERDADEIRO",
                                  "verdict_evidence_keys": [r["evidence_key"] for r in records],
                                  "explanation": "Proposta de teste com revisão humana pendente."})
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length" if mode == "truncated" else "stop",
                                message=SimpleNamespace(content=content, refusal="Não posso responder." if mode == "refused" else None))],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150))
    sdk.chat.completions.create.side_effect = complete
    return Client(cfg, sdk)


def wait_job(jobs, submitted):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = jobs.get(submitted["job_id"])
        if job["status"] != "PROCESSING":
            return job
        time.sleep(.01)
    raise AssertionError("background job did not finish")


@contextmanager
def local_server(jobs, directory):
    class TestHandler(Handler):
        pass
    TestHandler.evidence = jobs
    TestHandler.store = Store(directory)
    TestHandler.outbox = SimpleNamespace(state=lambda: {"configured": False})
    TestHandler.extra_outboxes = []
    http = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", TestHandler.store
    finally:
        http.shutdown(); http.server_close(); thread.join()


class AuditTests(unittest.TestCase):
    def run_analysis(self, directory, mode="valid", text=fixtures.MATERNITY, stale=False, long_cell=False):
        cfg = Config(enabled=True)
        source = fixtures.FakeSource()
        if long_cell:
            source.data[1][2][-1][13] += " Texto adicional." * 200
        clock = [0]
        search = Search(cfg, source, clock=lambda: clock[0])
        if stale:
            search.snapshot(); clock[0] = 301; source.failure = True
        service = Service(cfg, search, simulated_client(cfg, mode))
        jobs = Jobs(cfg, directory, service)
        submitted = jobs.submit({"segment_id": "block-42", "speaker": "Lula", "transcription": text})
        return jobs, wait_job(jobs, submitted)

    def test_exact_calls_rankings_gates_and_full_export(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OPENAI_API_KEY": "private-key-for-test"}):
            jobs, job = self.run_analysis(directory, long_cell=True)
            self.assertEqual(job["status"], "COMPLETED")
            events = job["audit"]["events"]
            self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(len([e for e in events if e["kind"] == "model_request"]), 2)
            self.assertEqual(len([e for e in events if e["kind"] == "model_response"]), 2)
            self.assertTrue(next(e for e in events if e["kind"] == "assessment_input")["truncations"])
            self.assertEqual(next(e for e in events if e["kind"] == "search_ranking")["candidates"][0]["record_id"], "LU106")
            decision = next(e for e in events if e["kind"] == "verdict_decision")
            self.assertEqual(decision["accepted"], "VERDADEIRO")
            self.assertTrue(all(c["status"] == "passed" for c in decision["checks"]))
            normal, full = jobs.export(job["job_id"]), jobs.export(job["job_id"], full=True)
            self.assertNotIn("evidence_snapshot", normal)
            self.assertTrue(full["coverage"]["full_workbook_included"])
            self.assertEqual(audit_trace.digest(full["evidence_snapshot"]), next(e for e in events if e["kind"] == "snapshot")["sha256"])
            self.assertNotIn("private-key-for-test", json.dumps(full))
            self.assertNotIn("credentials_path", full["run"]["audit"]["configuration"])
            self.assertEqual(len(jobs.detail(job["job_id"], after=events[-2]["seq"])["audit"]["events"]), 1)
            recovered = Jobs(Config(), directory).get(job["job_id"])
            self.assertEqual(recovered, job)

    def test_rejected_outputs_are_preserved(self):
        for mode, status, check in [("changed_quote", "ERROR", "exact_transcript_quote"),
                                    ("invalid_json", "ERROR", "json_schema"),
                                    ("invented_id", "PARTIAL", "evidence_references"),
                                    ("invented_basis", "PARTIAL", "exact_documentary_quote"),
                                    ("refused", "ERROR", "response_complete"),
                                    ("truncated", "ERROR", "response_complete")]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                _, job = self.run_analysis(directory, mode)
                self.assertEqual(job["status"], status)
                events = job["audit"]["events"]
                self.assertTrue(any(e["kind"] == "model_response" for e in events))
                self.assertTrue(any(e["kind"] == "check" and e["code"] == check and e["status"] == "failed" for e in events))

    def test_stale_snapshot_keeps_proposal_and_explains_withholding(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs, job = self.run_analysis(directory, stale=True)
            decision = next(e for e in job["audit"]["events"] if e["kind"] == "verdict_decision")
            self.assertEqual(decision["proposed"], "VERDADEIRO")
            self.assertIsNone(decision["accepted"])
            self.assertTrue(decision["withheld"])
            self.assertIn("fresh_cache", [c["code"] for c in decision["checks"] if c["status"] == "failed"])
            self.assertTrue(jobs.history()["jobs"][0]["withheld"])

    def test_no_claims_no_match_timeout_and_failed_refresh(self):
        for mode, text in [("no_claims", fixtures.MATERNITY), ("valid", "A produção de abacates em Marte triplicou."), ("timeout", fixtures.MATERNITY)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                _, job = self.run_analysis(directory, mode, text)
                if mode == "no_claims":
                    self.assertTrue(any(e["kind"] == "step_skipped" for e in job["audit"]["events"]))
                elif mode == "timeout":
                    self.assertEqual(job["status"], "ERROR")
                    self.assertNotIn("secret transport payload", json.dumps(job))
                else:
                    self.assertEqual(job["result"]["claims"][0]["retrieval_status"], "NO_MATCH")
                    self.assertFalse(any(e["kind"] == "assessment_input" for e in job["audit"]["events"]))
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(enabled=True); source = fixtures.FakeSource(); source.failure = True
            jobs = Jobs(cfg, directory, Service(cfg, Search(cfg, source), simulated_client(cfg)))
            job = wait_job(jobs, jobs.submit({"segment_id":"s", "speaker":"Lula", "transcription":fixtures.MATERNITY}))
            self.assertEqual(job["result"]["claims"][0]["retrieval_status"], "SEARCH_ERROR")
            self.assertTrue(any(e["kind"] == "step_finished" and e["status"] == "failed" for e in job["audit"]["events"]))

    def test_concurrent_traces_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(enabled=True)
            jobs = Jobs(cfg, directory, Service(cfg, Search(cfg, fixtures.FakeSource()), simulated_client(cfg, barrier=threading.Barrier(2))))
            inputs = ["Texto de teste exclusivo ALFA.", "Texto de teste exclusivo BETA."]
            submitted = [jobs.submit({"segment_id":str(i), "speaker":"Lula", "transcription":text}) for i,text in enumerate(inputs)]
            for i,item in enumerate(submitted):
                job = wait_job(jobs,item)
                requests = [e["request"] for e in job["audit"]["events"] if e["kind"] == "model_request"]
                self.assertEqual(json.loads(requests[0]["messages"][1]["content"])["transcription"], inputs[i])
                self.assertNotIn(inputs[1-i], json.dumps(job, ensure_ascii=False))

    def test_interrupted_and_legacy_history_and_path_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Jobs(Config(),directory); job_id = "a" * 32
            job = {"job_id":job_id,"segment_id":"s","status":"PROCESSING","input":{"transcription":"text"},"audit":{"events":[]}}
            jobs._write(jobs._path(job_id),job)
            with open(jobs._path(job_id,".events.jsonl"),"w",encoding="utf-8") as f:
                f.write(json.dumps({"seq":1,"kind":"step_started","child_step_id":"step"}) + '\n{"incomplete":')
            recovered = jobs.get(job_id)
            self.assertEqual(recovered["status"],"INTERRUPTED")
            self.assertEqual(len(recovered["audit"]["events"]),1)
            legacy = {"job_id":"b"*32,"segment_id":"old","status":"COMPLETED","input":{"transcription":"old"},"result":{"claims":[]}}
            jobs._write(jobs._path(legacy["job_id"]),legacy)
            self.assertEqual(len(jobs.history()["jobs"]),2)
            self.assertFalse(jobs.export(legacy["job_id"],full=True)["coverage"]["full_workbook_included"])
            self.assertIsNone(jobs.get("../.env"))

    def test_trace_write_failures_preserve_in_memory_results(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(enabled=True)
            jobs = Jobs(cfg, directory, Service(cfg, Search(cfg, fixtures.FakeSource()), simulated_client(cfg)))
            with patch.object(jobs, "_write", side_effect=OSError("disk full")):
                job = wait_job(jobs, jobs.submit({"segment_id":"s", "speaker":"Lula", "transcription":fixtures.MATERNITY}))
            self.assertEqual(job["status"], "COMPLETED")
            self.assertIn("persistence_error", job["audit"])
            self.assertTrue(job["audit"]["events"])
            self.assertEqual(jobs.export(job["job_id"])["run"]["result"], job["result"])

    def test_history_index_avoids_loading_completed_model_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs, job = self.run_analysis(directory)
            restored = Jobs(Config(), directory)
            with patch.object(restored, "get", side_effect=AssertionError("history should use summary index")):
                self.assertEqual(restored.history()["jobs"][0]["job_id"], job["job_id"])

    def test_http_assets_exports_freshness_and_separate_review(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs, job = self.run_analysis(directory)
            with local_server(jobs,directory) as (base,store):
                def get(path):
                    with urllib.request.urlopen(base + path,timeout=2) as response:
                        return json.load(response)
                for path in ("/debug","/debug.js","/debug.css"):
                    with urllib.request.urlopen(base + path,timeout=2) as response:
                        self.assertEqual(response.status,200)
                self.assertEqual(get("/api/ai-debug")["total"],1)
                detail = "/api/ai-debug/" + job["job_id"]
                self.assertTrue(get(detail + "/export?full=1")["coverage"]["full_workbook_included"])
                with urllib.request.urlopen(base + detail + "/export", timeout=2) as response:
                    self.assertIn("attachment;", response.headers["Content-Disposition"])
                    self.assertEqual(json.load(response)["schema_version"], "1.0")
                with open(store.blocks_path,"w",encoding="utf-8") as f:
                    f.write(json.dumps({"id":"block-42","text":"Edited later","final":True,"status":"ok"}) + "\n")
                self.assertTrue(get(detail)["input_changed"])
                original = jobs._path(job["job_id"]).read_bytes()
                body = json.dumps({"assessment":"error_found","notes":"Review note"}).encode()
                with urllib.request.urlopen(urllib.request.Request(base + detail + "/review",data=body,headers={"Content-Type":"application/json"})) as response:
                    self.assertEqual(json.load(response)["notes"],"Review note")
                self.assertEqual(jobs._path(job["job_id"]).read_bytes(),original)
                self.assertEqual(get(detail)["review"]["assessment"],"error_found")
                self.assertNotIn("audit", get("/api/ai-evidence/" + job["job_id"]))
                for path in ("/api/ai-debug/" + "0"*32,"/api/ai-debug/../../.env"):
                    with self.assertRaises(urllib.error.HTTPError): get(path)
                request = urllib.request.Request(base + "/api/ai-debug",headers={"Host":"attacker.example"})
                with self.assertRaises(urllib.error.HTTPError) as raised: urllib.request.urlopen(request)
                self.assertEqual(raised.exception.code,403)


if __name__ == "__main__":
    unittest.main()
