"""Evidence analysis orchestration and bounded background jobs for the operator UI."""
import argparse
import copy
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ai_config import Config
import audit_trace as audit


def now():
    return datetime.now(timezone.utc).isoformat()


def route_team(claim, evidence, topic_teams):
    from evidence_search import normalize
    relevant = [e for e in evidence if e.relevance in {"DIRECT_MATCH", "PARTIAL_MATCH"}]
    teams = sorted({t for e in relevant for t in re.findall(r"\b(?:F[1-4]|L[12])\b", e.team)})
    if not evidence:
        for topic, configured in topic_teams.items():
            if normalize(topic) and normalize(topic) in normalize(claim.topic):
                values = configured if isinstance(configured, list) else [configured]
                teams.extend(t for t in values if t in {"F1", "F2", "F3", "F4", "L1", "L2"})
        teams = sorted(set(teams))
    return (teams[0] if len(teams) == 1 else None), teams


class Service:
    def __init__(self, cfg, search=None, client=None):
        from evidence_search import Search
        self.cfg = cfg
        self.search = search or Search(cfg)
        self.client = client

    def analyze(self, value, refresh=False):
        from ai_client import AIError, Client
        from claim_extraction import extract
        from evidence_assessment import assess
        from evidence_search import SearchError
        from schemas import AnalysisResult, ClaimResult, Segment
        segment = Segment.model_validate(value)
        result = AnalysisResult(segment_id=segment.segment_id, speaker=segment.speaker, status="COMPLETED",
                                provider=self.cfg.provider, model=self.cfg.model, analyzed_at=now(),
                                transcription=segment.transcription)
        client = self.client or Client(self.cfg)
        try:
            with audit.step("Extrair afirmações", actor="Model"):
                claims = extract(client, segment)
        except AIError as exc:
            result.status, result.errors = "ERROR", [str(exc)]
            return result.model_dump()
        if not claims:
            for name in ("Consultar base", "Buscar e avaliar evidências", "Aplicar regras e encaminhar"):
                audit.skip(name, "Nenhuma afirmação verificável foi extraída.")
            return result.model_dump()
        audit.plan("Consultar base", actor="Search")
        for index in range(1, len(claims) + 1):
            claim_id = f"{segment.segment_id}_claim_{index}"
            audit.plan("Buscar evidências", actor="Search", claim_id=claim_id)
            audit.plan("Avaliar evidências e aplicar regras", actor="Model + Python", claim_id=claim_id)
            audit.plan("Encaminhar equipe", claim_id=claim_id)
        try:
            with audit.step("Consultar base", actor="Search", refresh_requested=refresh):
                records, cache_status, warning = self.search.snapshot(refresh)
                audit.snapshot(records, cache_status, warning)
            search_error = None
        except SearchError as exc:
            records, cache_status, warning, search_error = [], "FRESH", None, str(exc)
        for index, extracted in enumerate(claims, 1):
            claim = ClaimResult(**extracted.model_dump(), claim_id=f"{segment.segment_id}_claim_{index}",
                                segment_id=segment.segment_id, speaker=segment.speaker,
                                start_time=segment.start_time, end_time=segment.end_time,
                                retrieval_status="REVIEW_REQUIRED")
            if search_error:
                for name in ("Buscar evidências", "Avaliar evidências e aplicar regras", "Encaminhar equipe"):
                    audit.skip(name, "Busca indisponível; esta etapa não foi executada.", claim_id=claim.claim_id)
                claim.retrieval_status, claim.errors = "SEARCH_ERROR", [search_error]
                claim.explanation = "Busca indisponível; não é possível concluir se há registros relevantes."
            else:
                try:
                    with audit.step("Buscar evidências", actor="Search", claim_id=claim.claim_id):
                        claim.evidence = self.search.search(extracted, records, cache_status)
                        audit.emit("retrieved_evidence", evidence=[e.model_dump() for e in claim.evidence])
                    if warning:
                        claim.errors.append(warning)
                    if not claim.evidence:
                        audit.skip("Avaliar evidências e aplicar regras", "Nenhum candidato foi recuperado.", claim_id=claim.claim_id)
                        claim.retrieval_status = "REVIEW_REQUIRED" if cache_status == "STALE" else "NO_MATCH"
                        claim.explanation = "Nenhum candidato na base consultada. Isso não determina a veracidade da afirmação."
                    else:
                        with audit.step("Avaliar evidências e aplicar regras", actor="Model + Python", claim_id=claim.claim_id):
                            claim.evidence, claim.suggested_verdict, claim.explanation = assess(
                                client, {**extracted.model_dump(), "speaker": segment.speaker}, claim.evidence)
                        applicable = any(e.relevance in {"DIRECT_MATCH", "PARTIAL_MATCH"} for e in claim.evidence)
                        claim.retrieval_status = "MATCH_FOUND" if applicable and cache_status == "FRESH" else "REVIEW_REQUIRED"
                    with audit.step("Encaminhar equipe", claim_id=claim.claim_id):
                        claim.assigned_team, claim.team_candidates = route_team(extracted, claim.evidence, self.cfg.topic_teams)
                        audit.emit("team_routing", assigned_team=claim.assigned_team, team_candidates=claim.team_candidates)
                except (AIError, SearchError) as exc:
                    claim.errors.append(str(exc))
                    claim.explanation = "Análise incompleta; consulte os registros e tente novamente."
                    claim.retrieval_status = "SEARCH_ERROR" if isinstance(exc, SearchError) else "REVIEW_REQUIRED"
            if claim.errors:
                result.status = "PARTIAL"
            audit.emit("claim_result", claim_id=claim.claim_id, output=claim.model_dump())
            result.claims.append(claim)
        # Revalidate the complete public result after model_copy and orchestration.
        return AnalysisResult.model_validate(result.model_dump()).model_dump()


class Jobs:
    """Two concurrent jobs; durable audit history separate from delivery outboxes."""
    def __init__(self, cfg, data_dir, service=None):
        self.cfg, self.data_dir, self.service = cfg, data_dir, service
        self.lock = threading.Lock()
        self.jobs = {}
        self.directory = Path(data_dir) / "ai_evidence"

    def _path(self, job_id, suffix=".json"):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("identificador de análise inválido")
        return self.directory / (job_id + suffix)

    @staticmethod
    def _write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + ".tmp", "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(str(path) + ".tmp", path)

    def _record(self, job_id, event):
        with self.lock:
            events = self.jobs[job_id]["audit"]["events"]
            event = {"seq": len(events) + 1, **event}
            events.append(event)
            try:
                with open(self._path(job_id, ".events.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                self.jobs[job_id]["audit"]["persistence_error"] = "Não foi possível salvar todos os eventos no disco."

    def _attachment(self, job_id, value):
        try:
            self._write(self._path(job_id, ".snapshot.json"), value)
            with self.lock:
                self.jobs[job_id]["audit"]["coverage"]["full_evidence_snapshot"] = True
            return True
        except OSError:
            return False

    def submit(self, segment, refresh=False):
        if not self.cfg.enabled:
            raise ValueError("habilite AI_EVIDENCE_ENABLED em .env")
        # Validate before reserving a job or starting network activity.
        from schemas import Segment
        segment = Segment.model_validate(segment).model_dump()
        previous = next((j["job_id"] for j in self.history()["jobs"] if j["segment_id"] == segment["segment_id"]), None)
        with self.lock:
            if sum(j["status"] == "PROCESSING" for j in self.jobs.values()) >= 2:
                raise ValueError("duas análises já estão em andamento; aguarde")
            if len(self.jobs) >= 50:
                oldest = next(k for k, v in self.jobs.items() if v["status"] != "PROCESSING")
                del self.jobs[oldest]
            job_id = uuid.uuid4().hex
            job = {"job_id": job_id, "segment_id": segment["segment_id"], "status": "PROCESSING",
                   "input": segment, "result": None, "error": None,
                   "created_at": now(), "finished_at": None, "parent_job_id": previous,
                   "input_sha256": audit.digest(segment), "refresh_requested": refresh,
                   "audit": audit.metadata(self.cfg)}
            self.jobs[job_id] = job
            if self.service is None:
                self.service = Service(self.cfg)
            try:
                self._write(self._path(job_id), job)
                self._write(self._path(job_id, ".summary.json"), self._summary(job))
            except OSError:
                job["audit"]["persistence_error"] = "Histórico local indisponível; mantenha esta página aberta e exporte a análise."
        threading.Thread(target=self._run, args=(job_id, segment, refresh), daemon=True).start()
        return {"job_id": job_id, "status": "PROCESSING"}

    def _run(self, job_id, segment, refresh):
        trace = audit.Trace(lambda event: self._record(job_id, event), lambda value: self._attachment(job_id, value))
        with trace.activate():
            try:
                with audit.step("Análise do bloco", input_sha256=audit.digest(segment)) as outcome:
                    result = self.service.analyze(segment, refresh=refresh)
                    outcome["status"] = {"ERROR": "failed", "PARTIAL": "partial"}.get(result["status"], "completed")
                    audit.emit("analysis_result", status=result["status"])
                update = {"status": result["status"], "result": result}
            except Exception:
                update = {"status": "ERROR", "error": "não foi possível analisar; verifique dependências e configuração"}
            audit.emit("run_finished", status=update["status"])
        update["finished_at"] = now()
        with self.lock:
            snapshot = copy.deepcopy(self.jobs[job_id])
        snapshot.update(update)
        snapshot["audit"]["coverage"]["full_evidence_snapshot"] = self._path(job_id, ".snapshot.json").is_file()
        try:
            self._write(self._path(job_id), snapshot)
            self._write(self._path(job_id, ".summary.json"), self._summary(snapshot))
        except OSError:
            snapshot["audit"]["persistence_error"] = "Resultado disponível, mas não foi possível salvar o histórico local."
        with self.lock:
            self.jobs[job_id] = snapshot

    def get(self, job_id):
        try:
            path = self._path(job_id)
        except (ValueError, TypeError):
            return None
        with self.lock:
            if job_id in self.jobs:
                return copy.deepcopy(self.jobs[job_id])
        try:
            with open(path, encoding="utf-8") as f:
                job = json.load(f)
            if job.get("job_id") != job_id or not isinstance(job.get("input"), dict):
                return None
            if job.get("status") == "PROCESSING":
                events = []
                try:
                    with open(self._path(job_id, ".events.jsonl"), encoding="utf-8") as f:
                        for line in f:
                            try:
                                events.append(json.loads(line))
                            except ValueError:
                                break  # a crash may leave an incomplete final line
                except OSError:
                    pass
                job.setdefault("audit", {})["events"] = events
                job.update(status="INTERRUPTED", error="O servidor foi encerrado antes de concluir esta análise.")
            return job
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _summary(job):
        events = job.get("audit", {}).get("events", [])
        return {"job_id": job["job_id"], "segment_id": job.get("segment_id", ""), "status": job["status"],
                "created_at": job.get("created_at"), "finished_at": job.get("finished_at"),
                "speaker": job["input"].get("speaker", ""), "preview": job["input"].get("transcription", "")[:180],
                "model": job.get("audit", {}).get("configuration", {}).get("model") or (job.get("result") or {}).get("model"),
                "withheld": any(e["kind"] == "verdict_decision" and e.get("withheld") for e in events),
                "no_match": any(c.get("retrieval_status") == "NO_MATCH" for c in (job.get("result") or {}).get("claims", [])),
                "claim_count": len((job.get("result") or {}).get("claims", [])), "has_trace": bool(events)}

    def history(self, limit=100):
        limit = min(200, max(1, limit))
        with self.lock:
            live = {k: self._summary(v) for k, v in self.jobs.items()}
        try:
            paths = sorted((p for p in self.directory.glob("*.json") if re.fullmatch(r"[a-f0-9]{32}", p.stem)),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            paths = []
        total = len(set(live) | {p.stem for p in paths})
        for path in paths[:limit]:
            if path.stem not in live:
                try:
                    with open(self._path(path.stem, ".summary.json"), encoding="utf-8") as f:
                        summary = json.load(f)
                    if summary.get("status") == "PROCESSING":
                        recovered = self.get(path.stem)
                        summary = self._summary(recovered) if recovered else summary
                    live[path.stem] = summary
                except (OSError, ValueError, TypeError):
                    job = self.get(path.stem)
                    if job:
                        live[path.stem] = self._summary(job)
        jobs = sorted(live.values(), key=lambda j: j.get("created_at") or "", reverse=True)[:limit]
        return {"jobs": jobs, "total": total, "limit": limit, "truncated": total > len(jobs)}

    def detail(self, job_id, after=0):
        job = self.get(job_id)
        if not job:
            return None
        if "audit" in job:
            events = job["audit"].get("events", [])
            job["audit"]["last_seq"] = events[-1]["seq"] if events else 0
            job["audit"]["events"] = [e for e in events if e.get("seq", 0) > after]
        job["review"] = self.review(job_id)
        return job

    def review(self, job_id, value=None):
        path = self._path(job_id, ".review.json")
        if value is not None:
            if not self.get(job_id):
                raise ValueError("análise desconhecida")
            if not isinstance(value, dict) or value.get("assessment") not in ("unreviewed", "looks_correct", "needs_investigation", "error_found"):
                raise ValueError("avaliação inválida")
            notes = value.get("notes", "")
            if not isinstance(notes, str) or len(notes) > 8000:
                raise ValueError("anotações devem ter até 8000 caracteres")
            value = {"assessment": value["assessment"], "notes": notes, "updated_at": now()}
            # Review files are mutable; the original run and events stay unchanged.
            with self.lock:
                self._write(path, value)
            return value
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"assessment": "unreviewed", "notes": ""}

    def export(self, job_id, full=False):
        job = self.detail(job_id)
        if not job:
            return None
        bundle = {"schema_version": "1.0", "exported_at": now(), "run": job,
                  "review_prompt": audit.REVIEW_PROMPT,
                  "coverage": {"full_workbook_included": False, "external_documents_opened": False,
                               "run_complete": job["status"] in {"COMPLETED", "PARTIAL", "ERROR"},
                               "trace_available": bool(job.get("audit", {}).get("events")),
                               "limitations": ["Julgamentos do modelo não são verificação independente de fontes.",
                                               "Execuções antigas podem não ter rastreamento de etapas."]}}
        if full:
            try:
                with open(self._path(job_id, ".snapshot.json"), encoding="utf-8") as f:
                    bundle["evidence_snapshot"] = json.load(f)
                bundle["coverage"]["full_workbook_included"] = True
            except (OSError, ValueError):
                bundle["coverage"]["limitations"].append("Snapshot completo da base não foi capturado nesta execução.")
        else:
            bundle["coverage"]["limitations"].append("Sem a base completa não é possível avaliar a recuperação exaustiva de registros.")
        return bundle


def main():
    parser = argparse.ArgumentParser(description="Diagnóstico somente leitura da IA e das quatro abas")
    parser.add_argument("--check", action="store_true", required=True)
    args = parser.parse_args()
    from platform_client import load_dotenv
    load_dotenv()
    cfg = Config.from_env()
    from ai_client import Client
    from evidence_search import Search
    failed = False
    for name, check in (("IA", lambda: {"provider": cfg.provider, "model": cfg.model, "models": Client(cfg).check()}),
                        ("Sheets", lambda: {"records": len(Search(cfg).snapshot(refresh=True)[0])})):
        try:
            print(name + ": " + json.dumps(check(), ensure_ascii=True))
        except Exception as exc:
            failed = True
            print(name + ": " + str(exc))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
