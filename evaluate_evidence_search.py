"""Read-only, manually labelled retrieval checks against the configured workbook.

Run: python evaluate_evidence_search.py
No inference, embeddings or spreadsheet writes. Labels are deliberately small;
hit@k measures known records, not factual correctness or exhaustive recall.
"""
import json
from dataclasses import replace
from ai_config import Config
from evidence_search import Search
from platform_client import load_dotenv
from schemas import ExtractedClaim

QUERIES = [
    ("LU106", "Em 2009 Flávio foi o único dos 49 deputados a votar contra ampliar a licença-maternidade", []),
    ("LU106", "Só um parlamentar se opôs a dar dois meses adicionais às servidoras para cuidar dos filhos", ["licença-maternidade", "Alerj", "2009"]),
    ("N01", "Houve fraude na apuração do primeiro turno porque urnas antigas travaram a contagem", []),
    ("N01", "A pausa de cinquenta minutos na divulgação dos votos comprova fraude eleitoral", ["apuração", "urnas", "primeiro turno"]),
    ("PF16", "A dívida pública cresceu 13 pontos percentuais em relação ao PIB", []),
    ("PF16", "O endividamento do governo aumentou treze pontos do produto interno bruto em quatro anos", ["dívida pública", "13", "PIB"]),
    ("FB205", "A dívida pública cresceu 13 pontos percentuais em relação ao PIB", []),
    (None, "Flávio Lula Bolsonaro presidente", []),
    (None, "A produção de abacates em Marte triplicou", []),
]


def evaluate(search):
    records, cache_status, warning = search.snapshot(refresh=True)
    rows = []
    for expected, query, synonyms in QUERIES:
        claim = ExtractedClaim(quote=query, normalized_claim=query, checkable=True, subjects=[],
                               topic="", keywords=[], synonyms=synonyms)
        found = [e.record_id for e in search.search(claim, records, cache_status)]
        rank = found.index(expected) + 1 if expected in found else None
        rows.append({"expected": expected, "query": query, "rank": rank, "top_10": found})
    positives = [r for r in rows if r["expected"]]
    return {"cache_status": cache_status, "warning": warning, "records": len(records),
            "positive_queries": len(positives),
            "hit_at_5": sum(r["rank"] is not None and r["rank"] <= 5 for r in positives) / len(positives),
            "hit_at_10": sum(r["rank"] is not None and r["rank"] <= 10 for r in positives) / len(positives),
            "negative_queries_without_candidates": sum(not r["top_10"] for r in rows if r["expected"] is None),
            "queries": rows}


if __name__ == "__main__":
    load_dotenv()
    print(json.dumps(evaluate(Search(replace(Config.from_env(), max_candidates=10))), ensure_ascii=True, indent=2))
