"""One rich-text summary shared by the operator preview and Sheets delivery."""
from urllib.parse import urlsplit


def usable_job(job, block, speaker):
    """Never attach an unfinished assessment or one for different text/speaker."""
    if not job or job.get("status") != "COMPLETED" or job.get("error"):
        return False
    source, result = job.get("input", {}), job.get("result") or {}
    return (source.get("segment_id") == block["id"]
            and source.get("transcription") == block["text"]
            and source.get("speaker") == speaker
            and result.get("status") == "COMPLETED"
            and result.get("segment_id") == block["id"]
            and result.get("transcription") == block["text"]
            and result.get("speaker") == speaker
            and not result.get("errors")
            and not any(c.get("errors") for c in result.get("claims", [])))


def safe_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def selected_evidence(claim):
    candidates = [e for e in claim.get("evidence", [])
                  if e.get("relevance") in {"DIRECT_MATCH", "PARTIAL_MATCH"}
                  and e.get("source_quality") == "RELIABLE"
                  and (safe_url(e.get("spreadsheet_row_url"))
                       or any(safe_url(u) for u in e.get("source_urls", [])))]
    compatible = [e for e in candidates if e.get("context_compatible") is True]
    chosen = compatible or [e for e in candidates if e.get("context_compatible") is False]
    chosen.sort(key=lambda e: (e["relevance"] != "DIRECT_MATCH", -e.get("score", 0)))
    # A repeated source record must not consume two of the three slots.
    selected, seen = [], set()
    for e in chosen:
        key = e.get("evidence_key") or e.get("spreadsheet_row_url") or e.get("record_id")
        if key not in seen:
            selected.append(e)
            seen.add(key)
        if len(selected) == 3:
            break
    return selected


def build_ai_cell(results):
    """Return literal text plus Sheets formatting, with UTF-16 run offsets."""
    parts, runs, length = [], [], 0

    def add(text, **formatting):
        nonlocal length
        if not text:
            return
        runs.append({"startIndex": length, "format": formatting})
        parts.append(text)
        length += len(text.encode("utf-16-le")) // 2

    add("Avaliação da IA · revisão humana necessária\n", bold=True)
    for result_index, result in enumerate(results):
        if result_index:
            add("\n────────────────────\n")
        claims = result.get("claims", [])
        if not claims:
            add("Nenhuma afirmação factual verificável identificada.\n")
        for index, claim in enumerate(claims):
            if index:
                add("\n")
            add("Frase: ", bold=True)
            add(claim["quote"] + "\n")
            add("Veredicto sugerido: ", bold=True)
            add((claim.get("suggested_verdict") or "Sem veredicto") + "\n", bold=True)
            add("Evidências selecionadas:\n", bold=True)
            evidence = selected_evidence(claim)
            if not evidence:
                add("Nenhuma correspondência aplicável com fonte confiável.\n")
            for e in evidence:
                relevance = "direta" if e["relevance"] == "DIRECT_MATCH" else "parcial"
                add(f"• {e.get('record_id', '')} · {e.get('sheet_name', '')} · correspondência {relevance}")
                if e.get("context_compatible") is False:
                    add(" · CONTEXTO INCOMPATÍVEL — apenas referência", bold=True)
                add("\n")
                if safe_url(e.get("spreadsheet_row_url")):
                    add("Abrir registro original", link={"uri": e["spreadsheet_row_url"]})
                    add("\n")
                # Two documentary URLs per record keep the live summary short.
                urls = list(dict.fromkeys(u for u in [*e.get("primary_source_urls", []),
                                                      *e.get("source_urls", [])] if safe_url(u)))[:2]
                for source_index, url in enumerate(urls, 1):
                    add(f"Fonte documental {source_index}", link={"uri": url})
                    add("\n")
            add("Resposta da IA: ", bold=True)
            add((claim.get("explanation") or "Sem explicação disponível.") + "\n")
    text = "".join(parts)
    # Sheets limits each cell to 50,000 characters. Keep a margin and never split
    # a surrogate pair; the browser displays the very same shortened summary.
    if length > 49000:
        text = text.encode("utf-16-le")[:48000 * 2].decode("utf-16-le", errors="ignore")
        cutoff = len(text.encode("utf-16-le")) // 2
        runs = [r for r in runs if r["startIndex"] < cutoff]
        runs.append({"startIndex": cutoff, "format": {}})
        text += "\n[Resumo truncado; consulte a análise completa no Debug.]"
    return {"userEnteredValue": {"stringValue": text}, "textFormatRuns": runs,
            "userEnteredFormat": {"wrapStrategy": "WRAP"}}
