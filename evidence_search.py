"""Read-only Sheets snapshots and bounded Portuguese lexical retrieval."""
import math
import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from schemas import Evidence
import audit_trace as audit

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
TRACKING = ["ID", "Bloco provável", "Região (Bloco 1)", "Editoria", "Equipe responsável (plano)",
            "Tópico", "Subtópico", "Tipo de registro", "Data", "Onde / como surgiu", "Quem",
            "Declaração ou conteúdo", "Contexto", "O que os dados mostram",
            "Classificação da agência (escala original)", "Etiqueta (manual aletheiafact)",
            "Veredicto sugerido (categorias do plano)", "Nota da conversão", "Como pode aparecer no debate",
            "Dado-chave para resposta rápida", "Checagem / registro", "Link da checagem",
            "Fonte primária (o que consultar)", "Link da fonte primária", "Observações e links adicionais"]
NARRATIVES = ["ID", "Campo que impulsiona", "Narrativa (síntese)", "Frases e peças que circulam",
              "Quem impulsiona", "Alcance medido (e quem mediu)", "Período", "O que os dados mostram",
              "Etiqueta (manual aletheiafact)", "Veredicto sugerido (plano)", "Equipe(s) do plano",
              "Bloco provável", "Critérios de noticiabilidade (manual)", "Como pode aparecer no debate",
              "Checagem / registro", "Link", "Fonte primária ou decisão", "Link da fonte", "Links adicionais"]
PLANS = ["ID", "Candidato", "Editoria", "Equipe (plano operacional)", "Tópico", "Subtópico",
         "Tipo de registro", "Página do plano", "O que o plano diz", "O que os dados mostram",
         "Etiqueta (manual aletheiafact)", "Veredicto sugerido (plano)", "Nota", "Bloco provável",
         "Como pode aparecer no debate", "Checagem / registro", "Link", "Fonte primária (o que consultar)", "Link da fonte"]


@dataclass(frozen=True)
class Tab:
    sheet_id: int
    headers: list
    weights: dict
    secondary: str
    team: str
    record_type: str | None
    label: str
    sources: tuple
    primary_sources: tuple
    kind: str

    @property
    def last_column(self):
        return chr(64 + len(self.headers))


TABS = (
    Tab(837967820, TRACKING, {"L": 5, "G": 4, "F": 3, "T": 3, "N": 1}, "N", "E", "H", "P", ("V", "X", "Y"), ("X",), "tracking"),
    Tab(1277645709, TRACKING, {"L": 5, "G": 4, "F": 3, "T": 3, "N": 1}, "N", "E", "H", "P", ("V", "X", "Y"), ("X",), "tracking"),
    Tab(191544813, NARRATIVES, {"C": 5, "D": 4, "H": 1}, "H", "K", None, "I", ("P", "R", "S"), ("R",), "narratives"),
    Tab(485802486, PLANS, {"I": 5, "F": 4, "E": 3, "J": 1}, "J", "D", "G", "K", ("Q", "S"), ("S",), "plans"),
)
TAB_BY_ID = {t.sheet_id: t for t in TABS}
STOP = set("a o as os de da do das dos e em no na nos nas um uma que para por com se ao aos era foi ser ter tem mais como sobre contra entre apenas disse afirma afirmou governo brasil brasileiro brasileira lula flavio bolsonaro candidato presidente deputado pais dados tema assunto".split())


def normalize(value):
    return " ".join(re.findall(r"[a-z0-9]+", "".join(c for c in unicodedata.normalize("NFKD", str(value).lower()) if not unicodedata.combining(c))))


def tokens(value):
    return {t for t in normalize(value).split() if t not in STOP and (len(t) > 2 or t.isdigit())}


def valid_url(value):
    try:
        p = urlsplit(value)
        return p.scheme in {"http", "https"} and bool(p.hostname) and not p.username and not p.password
    except ValueError:
        return False


def urls(text):
    return list(dict.fromkeys(u for raw in re.findall(r"https?://[^\s<>\"']+", text)
                             if valid_url(u := raw.rstrip(".,;)]}"))))


def cell(row, col):
    i = ord(col) - 65
    return str(row[i]) if i < len(row) else ""


class SearchError(ValueError):
    pass


class SheetsSource:
    """Separate readonly credential scope; no write client or write API methods."""
    def __init__(self, cfg):
        self.cfg = cfg

    def load(self):
        try:
            import httplib2
            from google.oauth2.service_account import Credentials
            from google_auth_httplib2 import AuthorizedHttp
            from googleapiclient.discovery import build
            creds = Credentials.from_service_account_file(self.cfg.credentials_path, scopes=SCOPES)
            api = build("sheets", "v4", http=AuthorizedHttp(creds, http=httplib2.Http(timeout=20)),
                        cache_discovery=False).spreadsheets()
            meta = api.get(spreadsheetId=self.cfg.spreadsheet_id,
                           fields="sheets(properties(sheetId,title))").execute(num_retries=0)
            names = {s["properties"]["sheetId"]: s["properties"]["title"] for s in meta["sheets"]}
            if any(tab.sheet_id not in names for tab in TABS):
                raise SearchError("uma das quatro abas configuradas está ausente")
            ranges = ["'" + names[t.sheet_id].replace("'", "''") + f"'!A1:{t.last_column}{self.cfg.max_rows_per_tab + 1}" for t in TABS]
            batch = api.values().batchGet(spreadsheetId=self.cfg.spreadsheet_id, ranges=ranges,
                                         valueRenderOption="FORMATTED_VALUE").execute(num_retries=0)
            value_ranges = batch.get("valueRanges", [])
            if len(value_ranges) != len(TABS):
                raise SearchError("resposta incompleta ao ler as quatro abas")
            return [(tab, names[tab.sheet_id], values.get("values", [])) for tab, values in zip(TABS, value_ranges)]
        except SearchError:
            raise
        except Exception as exc:
            code = getattr(getattr(exc, "resp", None), "status", None)
            if code in {403, 404}:
                raise SearchError("acesso à base de evidências negado ou planilha ausente; compartilhe a planilha com o client_email da conta de serviço como Leitor e habilite a API Sheets") from exc
            raise SearchError("falha ao ler a base de evidências; verifique credenciais, rede e API Sheets") from exc


class Search:
    def __init__(self, cfg, source=None, clock=time.monotonic):
        self.cfg, self.source, self.clock = cfg, source or SheetsSource(cfg), clock
        self.lock = threading.Lock()
        self.records = None
        self.loaded_at = None
        self.expires = 0
        self.retry_after = 0

    def snapshot(self, refresh=False):
        with self.lock:
            now = self.clock()
            if not refresh and self.records is not None:
                if now < self.expires:
                    return self.records, "FRESH", None
                if now < self.retry_after:
                    return self.records, "STALE", "cache anterior: atualização indisponível"
            try:
                records = []
                loaded = self.source.load()
                if {tab.sheet_id for tab, _, _ in loaded} != set(TAB_BY_ID) or len(loaded) != 4:
                    raise SearchError("snapshot não contém as quatro abas")
                retrieved_at = datetime.now(timezone.utc).isoformat()
                for tab, name, rows in loaded:
                    if len(rows) > self.cfg.max_rows_per_tab:
                        raise SearchError("base excede AI_EVIDENCE_MAX_ROWS_PER_TAB; aumente o limite")
                    if not rows or [normalize(x) for x in rows[0]] != [normalize(x) for x in tab.headers]:
                        raise SearchError(f"schema alterado na aba {name} ({tab.sheet_id}); revise o mapeamento")
                    seen = set()
                    for row_number, row in enumerate(rows[1:], 2):
                        record_id = cell(row, "A").strip()
                        if not re.fullmatch(r"[A-Za-z]{1,6}\d+", record_id):
                            continue  # headings, editorias and section labels
                        if record_id in seen:
                            raise SearchError(f"ID duplicado na aba {name}: {record_id}")
                        seen.add(record_id)
                        if any(len(str(v)) > 12000 for v in row):
                            raise SearchError("célula acima do limite de 12000 caracteres")
                        cell_range = f"A{row_number}:{tab.last_column}{row_number}"
                        fields = {h: cell(row, chr(65 + i)) for i, h in enumerate(tab.headers)}
                        evidence = Evidence(
                            evidence_key=f"{tab.sheet_id}:{record_id}", spreadsheet_id=self.cfg.spreadsheet_id,
                            sheet_id=tab.sheet_id, sheet_name=name, record_id=record_id, row_number=row_number,
                            cell_range=cell_range,
                            spreadsheet_row_url=f"https://docs.google.com/spreadsheets/d/{self.cfg.spreadsheet_id}/edit#gid={tab.sheet_id}&range={cell_range}",
                            fields=fields, team=cell(row, tab.team), existing_label=cell(row, tab.label),
                            record_type=cell(row, tab.record_type) if tab.record_type else "Narrativa; requer avaliação documental",
                            source_urls=urls(" ".join(cell(row, c) for c in tab.sources)),
                            primary_source_urls=urls(" ".join(cell(row, c) for c in tab.primary_sources)),
                            score=0.0, retrieved_at=retrieved_at, cache_status="FRESH",
                        )
                        records.append((tab, row, evidence))
                self.records, self.loaded_at = records, retrieved_at
                self.expires = self.clock() + self.cfg.cache_ttl_seconds
                self.retry_after = 0
                return records, "FRESH", None
            except Exception as exc:
                self.retry_after = self.clock() + min(30, max(1, self.cfg.cache_ttl_seconds))
                if self.records is not None:
                    return self.records, "STALE", str(exc) if isinstance(exc, SearchError) else "cache anterior: atualização indisponível"
                if isinstance(exc, SearchError):
                    raise
                raise SearchError("base indisponível; nenhum cache válido") from exc

    def search(self, claim, records, cache_status):
        # Never search metadata/classification/source fields. Names alone cannot match.
        query = tokens(" ".join([claim.normalized_claim, *claim.keywords, *claim.synonyms]))
        if not query:
            audit.emit("search_ranking", query_tokens=[], candidates=[], total_records=len(records),
                       eligible_count=0, excluded_overlap_count=len(records), max_candidates=self.cfg.max_candidates)
            return []
        docs, frequency = [], Counter()
        for tab, row, evidence in records:
            fields = {c: tokens(cell(row, c)) for c in tab.weights}
            all_tokens = set().union(*fields.values())
            frequency.update(all_tokens)
            docs.append((tab, row, evidence, fields, all_tokens))
        phrases = [normalize(k) for k in [*claim.keywords, *claim.synonyms] if len(tokens(k)) >= 2]
        ranked, explanations, excluded_overlap = [], {}, 0
        for tab, row, evidence, fields, all_tokens in docs:
            overlap = query & all_tokens
            if len(overlap) < 2:
                excluded_overlap += 1
                continue
            weights = dict(tab.weights)
            overrides = self.cfg.weights.get(tab.kind, {})
            for c, w in overrides.items():
                if c not in weights or not isinstance(w, (int, float)) or not math.isfinite(w) or not 0 <= w <= 20:
                    raise SearchError("pesos de busca inválidos")
                weights[c] = w
            score, contributions = 0.0, []
            for col, terms in fields.items():
                matched = terms & query
                if not matched:
                    continue
                # IDF and mild field-length normalization reduce generic matches.
                part = sum(math.log(1 + (len(docs) + 1) / (frequency[t] + 1)) for t in matched)
                part /= 1 + 0.015 * len(terms)
                matched_phrases = [p for p in phrases if p in normalize(cell(row, col))]
                part += 2 * len(matched_phrases)
                score += weights[col] * part
                if audit.active():
                    contributions.append({"column": col, "field": tab.headers[ord(col) - 65],
                                          "tokens": sorted(matched), "phrases": matched_phrases,
                                          "weight": weights[col], "contribution": round(weights[col] * part, 4)})
            if score <= 0:
                continue
            overlap_multiplier = 0.5 + len(overlap) / len(query)
            score *= overlap_multiplier
            # Metadata only breaks close ties; it never creates a lexical match.
            tie_break = tab.kind == "plans" and any(normalize(s) in normalize(cell(row, "B")) for s in claim.subjects if s)
            if tie_break:
                score *= 1.05
            ranked.append(evidence.model_copy(update={"score": round(score, 4), "cache_status": cache_status}))
            if audit.active():
                explanations[evidence.evidence_key] = {"fields": contributions, "overlap_tokens": sorted(overlap),
                                                       "overlap_multiplier": overlap_multiplier,
                                                       "subject_multiplier": 1.05 if tie_break else 1.0}
        ranked.sort(key=lambda r: (-r.score, r.sheet_id, r.record_id))
        if audit.active():
            audit.emit("search_ranking", query_tokens=sorted(query), phrases=phrases, total_records=len(records),
                       minimum_overlap=2, excluded_overlap_count=excluded_overlap, eligible_count=len(ranked),
                       max_candidates=self.cfg.max_candidates, scores_are_probabilities=False,
                       omitted_ranking_count=max(0, len(ranked) - 200),
                       candidates=[{"evidence_key": e.evidence_key, "record_id": e.record_id,
                                    "rank": i, "score": e.score, "selected": i <= self.cfg.max_candidates,
                                    **explanations[e.evidence_key]} for i, e in enumerate(ranked[:200], 1)])
        return ranked[:self.cfg.max_candidates]
