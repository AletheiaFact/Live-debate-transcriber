"""Editorial protocol and deterministic minimum gates, never label conversion."""
from evidence_search import TAB_BY_ID, normalize
import audit_trace as audit

PROTOCOL = """Veredictos permitidos, sempre provisórios:
VERDADEIRO: fonte primária/confiável confirma o núcleo no período, universo e
contexto corretos, sem omissão relevante que comprometa o sentido.
FALSO: fonte confiável contradiz diretamente a proposição central OU revela uso
de dado real sem contexto essencial, recorte inadequado ou inferência sem base.
Identifique a afirmação exata, o dado contrário ou o contexto ausente.
IMPRECISO: erro demonstrável de valor, data, termo ou detalhe cuja correção
preserva a informação principal. Diga o erro, a correção e por que o núcleo permanece.
INSUFICIENTE (EVIDÊNCIAS INSUFICIENTES): busca documentada nas fontes pertinentes
não confirma nem refuta; registre fontes examinadas e o dado faltante. Apenas
humanos finalizam. Falta de resultado na base NÃO implica INSUFICIENTE.
Não converta automaticamente etiquetas antigas, incluindo Enganoso, Confiável,
Confiável mas, Exagerado ou Não é fato. Etiqueta não é evidência.
Promessa, proposta, opinião e previsão não recebem veredicto factual.
"""


def permit_verdict(assessment, evidence):
    """Require a direct, compatible match with documentary basis beyond a label."""
    checks = []

    def gate(code, passed, detail, key=None):
        checks.append({"code": code, "status": "passed" if passed else "failed",
                       "detail": detail, "evidence_key": key})

    def finish(verdict):
        audit.emit("verdict_decision", proposed=assessment.suggested_verdict, accepted=verdict,
                   verdict_evidence_keys=assessment.verdict_evidence_keys, checks=checks,
                   withheld=assessment.suggested_verdict is not None and verdict is None)
        return verdict

    if assessment.suggested_verdict is None:
        checks.append({"code": "proposal", "status": "not_evaluated", "detail": "O modelo não propôs veredicto."})
        return finish(None)
    # v1 does not fetch documents or perform an exhaustive source investigation.
    gate("supported_protocol", assessment.suggested_verdict != "INSUFICIENTE", "INSUFICIENTE exige investigação documental fora deste serviço.")
    gate("supporting_records", bool(assessment.verdict_evidence_keys), "A proposta precisa indicar registros de apoio.")
    records = {e.evidence_key: e for e in evidence}
    evaluated = {r.evidence_key: r for r in assessment.records}
    for key in assessment.verdict_evidence_keys:
        e, a = records.get(key), evaluated.get(key)
        gate("known_record", bool(e and a), "O registro e sua avaliação devem existir.", key)
        if not e or not a:
            continue
        gate("direct_match", a.relevance == "DIRECT_MATCH", "O modelo deve declarar correspondência direta (julgamento da IA).", key)
        gate("compatible_context", a.context_compatible, "O modelo deve declarar contexto compatível (julgamento da IA).", key)
        gate("reliable_source", a.source_quality == "RELIABLE", "O modelo deve declarar fonte confiável (sem autenticação externa).", key)
        tab = TAB_BY_ID[e.sheet_id]
        kind = normalize(e.record_type)
        blocked = ("antecip", "preliminar", "pendente", "promessa", "proposta", "objetivo", "omissao", "contexto", "background")
        gate("eligible_record_type", not any(t in kind for t in blocked), "Tipo do registro não pode ser preliminar, proposta, promessa ou contexto.", key)
        # Candidate/plan records must explicitly identify a checked factual declaration.
        gate("checked_factual_record", tab.kind == "narratives" or any(t in kind for t in ("chec", "declaracao", "afirmacao factual", "diagnostico com dado")),
             "Registro deve identificar uma declaração factual ou checagem.", key)
        basis = e.fields[tab.headers[ord(tab.secondary) - 65]]
        gate("source_url", bool(e.source_urls), "Registro precisa conter URL documental; o documento não foi aberto.", key)
        gate("documentary_basis", len(a.basis_quote.strip()) >= 24 and a.basis_quote in basis,
             "Trecho de apoio deve existir no campo documental e ter ao menos 24 caracteres.", key)
        gate("fresh_cache", e.cache_status != "STALE", "A base consultada precisa estar atualizada.", key)
    return finish(assessment.suggested_verdict if all(c["status"] == "passed" for c in checks) else None)
