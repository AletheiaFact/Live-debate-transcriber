"use strict";
const $ = selector => document.querySelector(selector);
const STATUS = {PROCESSING:"Em andamento",COMPLETED:"Concluída",PARTIAL:"Parcial",ERROR:"Falha",INTERRUPTED:"Interrompida"};
const RELEVANCE = {DIRECT_MATCH:"Direta",PARTIAL_MATCH:"Parcial",BACKGROUND_ONLY:"Contexto",NOT_RELEVANT:"Não relevante"};
const RETRIEVAL = {MATCH_FOUND:"Registro aplicável encontrado",NO_MATCH:"Sem correspondência",REVIEW_REQUIRED:"Revisão necessária",SEARCH_ERROR:"Erro na busca"};
let summaries = [], currentRun = null, selectedId = null, selectedStep = null, selectedEvidence = null;
let inspectorTab = "overview", selectionVersion = 0, lastRenderKey = "", reviewDirty = false, pollBusy = false;
const reviewDrafts = new Map();

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function clear(node) { node.replaceChildren(); }
function pretty(value) { return JSON.stringify(value, null, 2); }
function badge(text, kind) { return el("span", text, `badge ${(kind || "").toLowerCase()}`); }
function shortTime(value) {
  if (!value) return "Horário não registrado";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("pt-BR", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
function duration(ms) { return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`; }
function notice(text) { $("#notice").textContent = text; $("#notice").hidden = !text; }
async function api(path, options) {
  const response = await fetch(path, {cache:"no-store", ...options});
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `Não foi possível consultar o servidor (HTTP ${response.status}).`);
  }
  return response.json();
}
function events(run = currentRun) { return run?.audit?.events || []; }
function steps(run = currentRun) {
  const list = events(run);
  const actual = list.filter(e => e.kind === "step_started" || e.kind === "step_skipped").map(e => {
    if (e.kind === "step_skipped") return {...e, id:`skip-${e.seq}`, status:"skipped"};
    const end = list.find(x => x.kind === "step_finished" && x.step_id === e.child_step_id);
    return {...e, id:e.child_step_id, status:end?.status || (run.status === "PROCESSING" ? "processing" : "interrupted"), duration_ms:end?.duration_ms, error_message:end?.error_message};
  });
  // Placeholders are replaced by their real step when work starts. Show future claims, too.
  const plans = list.filter(e => e.kind === "step_planned");
  for(const plan of plans) {
    const stage = actual.find(s => s.name === plan.name && s.claim_id === plan.claim_id);
    if(stage) stage.order = plan.seq;
    else actual.push({...plan,id:`plan-${plan.seq}`,order:plan.seq,status:run.status === "PROCESSING" ? "pending" : "not_evaluated"});
  }
  // Nested model calls follow their parent even when the parent was planned earlier.
  const roots = actual.filter(s => !s.parent_step_id || !actual.some(p => p.id === s.parent_step_id));
  const result = [];
  function visit(stage) {
    result.push(stage);
    for(const child of actual.filter(s => s.parent_step_id === stage.id).sort((a,b) => (a.order || a.seq) - (b.order || b.seq))) visit(child);
  }
  for(const root of roots.sort((a,b) => (a.order || a.seq) - (b.order || b.seq))) visit(root);
  return result;
}
function stepEvents(id, run = currentRun) {
  const ids = new Set([id]);
  const all = steps(run);
  let changed = true;
  while (changed) {
    changed = false;
    for (const s of all) if (ids.has(s.parent_step_id) && !ids.has(s.id)) { ids.add(s.id); changed = true; }
  }
  return events(run).filter(e => ids.has(e.step_id) || ids.has(e.child_step_id));
}
function claimEvents(claimId, run = currentRun) {
  const ids = steps(run).filter(s => s.claim_id === claimId).map(s => s.id);
  const found = new Map();
  for (const id of ids) for (const event of stepEvents(id, run)) found.set(event.seq, event);
  return [...found.values()].sort((a,b) => a.seq - b.seq);
}
function verdictDecision(claimId, run = currentRun) { return claimEvents(claimId, run).find(e => e.kind === "verdict_decision"); }
function allClaims(run = currentRun) {
  return run?.result?.claims || events(run).filter(e => e.kind === "claim_result").map(e => e.output);
}
function snapshotHash(run) { return events(run).find(e => e.kind === "snapshot")?.sha256 || null; }

function renderRuns() {
  const query = $("#searchRuns").value.toLocaleLowerCase("pt-BR");
  const filter = $("#filterRuns").value;
  const visible = summaries.filter(j => (!query || `${j.segment_id} ${j.speaker} ${j.preview} ${j.model}`.toLocaleLowerCase("pt-BR").includes(query)) &&
    (filter === "all" || (filter === "errors" ? ["ERROR","PARTIAL","INTERRUPTED"].includes(j.status) :
      filter === "withheld" ? j.withheld : filter === "no_match" ? j.no_match : j.status === filter)));
  const list = $("#runs"); clear(list);
  if (!visible.length) list.append(el("p", summaries.length ? "Nenhuma execução com este filtro." : "As análises aparecerão aqui quando você usar Analisar com IA.", "hint"));
  for (const run of visible) {
    const button = el("button", null, `run-item${run.job_id === selectedId ? " selected" : ""}`);
    button.setAttribute("aria-pressed", String(run.job_id === selectedId));
    const title = el("span", null, "run-name");
    title.append(el("span", `Bloco ${run.segment_id}`), badge(STATUS[run.status] || run.status, run.status));
    button.append(title, el("span", run.preview || "Sem texto", "run-preview"), el("span", `${run.speaker || "Sem falante"} · ${shortTime(run.created_at)}`, "run-time"));
    button.addEventListener("click", () => selectRun(run.job_id)); list.append(button);
  }
}

async function selectRun(id) {
  if(selectedId && reviewDirty) reviewDrafts.set(selectedId,{assessment:$("#reviewAssessment").value,notes:$("#reviewNotes").value});
  selectedId = id; selectedStep = null; selectedEvidence = null; currentRun = null;
  selectionVersion++; lastRenderKey = ""; reviewDirty = reviewDrafts.has(id);
  $("#comparison").replaceChildren(); $("#reviewSaved").textContent = "";
  const url = new URL(location.href); url.searchParams.set("run", id); history.replaceState(null, "", url);
  $("#welcome").hidden = true; $("#runView").hidden = true;
  $("#inspectorTitle").textContent = "Carregando execução…";
  $("#inspectorContent").replaceChildren(el("p", "Consultando o histórico local…", "hint"));
  renderRuns();
  try { await updateSelected(); notice(""); } catch (error) { notice(error.message); }
}

async function updateSelected() {
  if (!selectedId) return;
  const id = selectedId, version = selectionVersion;
  const after = currentRun?.audit?.last_seq || 0;
  const data = await api(`/api/ai-debug/${encodeURIComponent(id)}?after=${after}`);
  if (version !== selectionVersion || id !== selectedId) return;
  if (currentRun && after && data.audit) {
    const existing = new Map(events(currentRun).map(e => [e.seq,e]));
    for (const event of data.audit.events || []) existing.set(event.seq,event);
    data.audit.events = [...existing.values()].sort((a,b) => a.seq - b.seq);
  }
  currentRun = data;
  const renderKey = `${id}:${data.audit?.last_seq || 0}:${data.status}:${data.input_changed}:${data.audit?.persistence_error || ""}`;
  if (renderKey !== lastRenderKey) { renderRun(); lastRenderKey = renderKey; }
}

function renderRun() {
  const run = currentRun;
  $("#welcome").hidden = true; $("#runView").hidden = false;
  $("#runTitle").textContent = `Bloco ${run.segment_id}`;
  $("#runStatus").textContent = STATUS[run.status] || run.status;
  $("#runStatus").className = `badge ${run.status.toLowerCase()}`;
  const cfg = run.audit?.configuration || run.result || {};
  const calls = events().filter(e => e.kind === "model_request").length;
  $("#runMeta").textContent = `${cfg.provider || "Provedor não registrado"} / ${cfg.model || "modelo não registrado"} · ${shortTime(run.created_at || run.result?.analyzed_at)} · ${calls} ${calls === 1 ? "chamada" : "chamadas"} ao modelo · ${run.job_id.slice(0,8)}`;
  $("#speaker").textContent = run.input.speaker || "Sem falante atribuído";
  const warnings = [run.error, ...(run.result?.errors || []), run.audit?.persistence_error];
  if (!events().length) warnings.push("Execução anterior ao dashboard: o resultado foi preservado, mas as etapas e chamadas originais não foram capturadas.");
  if (run.input_changed) warnings.push("A transcrição ou o falante mudou. Esta execução continua mostrando o texto original analisado.");
  if (run.status === "PROCESSING") warnings.push("Análise em andamento. Uma exportação agora será parcial.");
  $("#runWarning").textContent = warnings.filter(Boolean).join("\n");
  $("#runWarning").hidden = !warnings.some(Boolean);
  $("#exportRun").href = `/api/ai-debug/${run.job_id}/export`;
  $("#exportRun").download = `audit-${run.job_id}.json`;
  $("#exportFull").href = `/api/ai-debug/${run.job_id}/export?full=1`;
  $("#exportFull").download = `audit-${run.job_id}-full.json`;
  renderTranscript(); renderTimeline(); renderClaims(); renderInspector(); renderCompareOptions();
  if (reviewDrafts.has(run.job_id)) {
    $("#reviewAssessment").value = reviewDrafts.get(run.job_id).assessment;
    $("#reviewNotes").value = reviewDrafts.get(run.job_id).notes;
  } else if (!reviewDirty) {
    $("#reviewAssessment").value = run.review?.assessment || "unreviewed";
    $("#reviewNotes").value = run.review?.notes || "";
  }
}

function selectedClaimId() {
  if (selectedEvidence) return selectedEvidence.claimId;
  let stage = steps().find(s => s.id === selectedStep);
  const seen = new Set();
  while (stage && !seen.has(stage.id)) {
    if (stage.claim_id) return stage.claim_id;
    seen.add(stage.id); stage = steps().find(s => s.id === stage.parent_step_id);
  }
  return null;
}
function renderTranscript() {
  const node = $("#transcript"), text = currentRun.input.transcription;
  clear(node);
  const selectedClaim = allClaims().find(c => c.claim_id === selectedClaimId());
  const at = selectedClaim ? text.indexOf(selectedClaim.quote) : -1;
  if (at < 0) node.textContent = text;
  else node.append(document.createTextNode(text.slice(0,at)), el("mark", selectedClaim.quote), document.createTextNode(text.slice(at + selectedClaim.quote.length)));
}
function renderTimeline() {
  const list = $("#timeline"), all = steps(); clear(list);
  const finished = all.filter(s => !["pending","processing"].includes(s.status)).length;
  const pending = all.filter(s => s.status === "pending").length;
  $("#progress").textContent = all.length ? `${finished} encerradas · ${pending} pendentes` : "Etapas não registradas";
  if (!all.length) list.append(el("p", "Consulte o resultado e as evidências abaixo. Novas análises incluem a linha do tempo.", "hint"));
  for (const stage of all) {
    const button = el("button", null, `step ${stage.status}${stage.parent_step_id ? " nested" : ""}${stage.id === selectedStep && !selectedEvidence ? " selected" : ""}`);
    button.setAttribute("aria-pressed", String(stage.id === selectedStep && !selectedEvidence));
    const symbol = {completed:"✓",partial:"!",failed:"!",processing:"◉",pending:"○",not_evaluated:"—",skipped:"↷",interrupted:"□"}[stage.status];
    const body = el("span", null, "step-body");
    body.append(el("span",stage.name,"step-name"),el("span",stage.reason || `${stage.actor || "Python"}${stage.claim_id ? " · " + stage.claim_id : ""}`,"step-sub"));
    button.append(el("span",symbol,"step-symbol"),body,el("span",stage.duration_ms !== undefined ? duration(stage.duration_ms) : ({processing:"Em andamento",pending:"Aguardando",not_evaluated:"Não executada",skipped:"Ignorada",interrupted:"Interrompida"}[stage.status] || ""),"step-duration"));
    button.addEventListener("click", () => { selectedStep = stage.id; selectedEvidence = null; renderTimeline(); renderTranscript(); renderInspector(); });
    list.append(button);
  }
}
function safeLink(parent,url,label) {
  try {
    const parsed = new URL(url);
    if (!["http:","https:"].includes(parsed.protocol) || parsed.username || parsed.password) return;
    const link = el("a",label); link.href = parsed.href; link.target = "_blank"; link.rel = "noopener noreferrer"; parent.append(link);
  } catch { /* malformed source links are not made clickable */ }
}
function renderClaims() {
  const node = $("#claims"), claims = allClaims(); clear(node);
  if (!claims.length) {
    const extracted = events().find(e => e.kind === "extraction")?.output?.claims;
    node.append(el("p", currentRun.status === "PROCESSING" ? (extracted ? `${extracted.length} afirmações extraídas. As evidências aparecerão conforme cada avaliação terminar.` : "Aguardando extração de afirmações…") : currentRun.status === "COMPLETED" ? "Nenhuma afirmação verificável foi identificada." : "Não foi possível concluir a extração ou avaliação. Consulte as etapas e a saída original.", "hint"));
  }
  for (const claim of claims) {
    const article = el("article",null,"claim");
    article.append(el("h4",`“${claim.quote}”`,"claim-quote"),el("p",`Proposição normalizada: ${claim.normalized_claim}`,"claim-normalized"));
    const meta = el("div",null,"claim-meta"), decision = verdictDecision(claim.claim_id);
    meta.append(badge(RETRIEVAL[claim.retrieval_status] || claim.retrieval_status || "", ""),badge(`Resultado: ${claim.suggested_verdict || "sem veredicto"}`,""));
    if (decision?.withheld) meta.append(badge(`Proposta retida: ${decision.proposed}`,"withheld"));
    article.append(meta,el("p",claim.explanation,"claim-explanation"),el("p",`Equipe: ${claim.assigned_team || (claim.team_candidates?.length ? "ambígua: " + claim.team_candidates.join(", ") : "a definir")} · Revisão humana pendente`,"hint"));
    for (const error of claim.errors || []) article.append(el("p",error,"warning"));
    if (claim.evidence?.length) {
      const wrapper = el("div",null,"table-scroll"), table = el("table"), head = el("thead"), tr = el("tr");
      for (const title of ["Registro / busca","Relevância (IA)","Contexto (IA)","Fonte (IA)","Trecho em Python","Regra do veredicto"]) tr.append(el("th",title));
      head.append(tr); table.append(head); const tbody = el("tbody");
      for (const [index,e] of claim.evidence.entries()) {
        const row = el("tr"), first = el("td"), inspect = el("button",e.record_id);
        inspect.addEventListener("click", () => { selectedEvidence = {claimId:claim.claim_id,record:e}; renderTimeline(); renderTranscript(); renderInspector(); });
        first.append(inspect,el("div",`#${index+1} · ${e.score ?? "—"}`,"hint"));
        const checks = decision?.checks.filter(c => !c.evidence_key || c.evidence_key === e.evidence_key) || [];
        const failed = checks.find(c => c.status === "failed");
        const exactCheck = claimEvents(claim.claim_id).find(x => x.kind === "check" && x.code === "exact_documentary_quote" && x.evidence_key === e.evidence_key);
        const basisLabel = exactCheck ? (exactCheck.status === "failed" ? "Rejeitado" : e.basis_quote ? "Exato" : "Sem trecho") : "Não registrado";
        row.append(first,el("td",RELEVANCE[e.relevance] || e.relevance),el("td",e.context_compatible === null || e.context_compatible === undefined ? "Não avaliado" : e.context_compatible ? "Compatível" : "Incompatível"),el("td",e.source_quality === "RELIABLE" ? "Confiável" : e.source_quality === "UNCERTAIN" ? "Incerta" : "Não avaliada"),el("td",basisLabel),el("td",failed?.detail || (checks.length ? "Regras passaram; exige revisão" : "Não usado para veredicto")));
        tbody.append(row);
      }
      table.append(tbody); wrapper.append(table); article.append(wrapper);
      for (const e of claim.evidence) {
        const details = el("details",null,"evidence-details"); details.append(el("summary",`${e.record_id} · ${e.sheet_name} · Abrir evidência e fontes`));
        details.append(el("p",e.summary),el("p",`Cache: ${e.cache_status} · Consultado em ${shortTime(e.retrieved_at)}`,"hint"));
        if (e.basis_quote) details.append(el("p",`Trecho usado pela IA: “${e.basis_quote}”`));
        safeLink(details,e.spreadsheet_row_url,"Abrir linha original");
        for (const [i,url] of (e.source_urls || []).entries()) safeLink(details,url,`Fonte documental ${i+1}`);
        for (const [i,url] of (e.primary_source_urls || []).entries()) safeLink(details,url,`Fonte primária ${i+1}`);
        const fields = el("dl"); for (const [key,value] of Object.entries(e.fields || {})) if(value) fields.append(el("dt",key),el("dd",value));
        details.append(fields); article.append(details);
      }
    }
    node.append(article);
  }
}

function jsonSection(parent,title,value) { parent.append(el("h3",title),el("pre",pretty(value))); }
function showRules(parent,list) {
  if (!list.length) parent.append(el("p","Nenhuma regra registrada nesta etapa.","hint"));
  const priority = {failed:0,not_evaluated:1,passed:2};
  for (const rule of [...list].sort((a,b) => (priority[a.status] ?? 3) - (priority[b.status] ?? 3))) {
    const box = el("div",null,"rule");
    box.append(badge({passed:"Passou",failed:"Falhou",not_evaluated:"Não avaliada"}[rule.status] || rule.status,rule.status),el("strong",` ${rule.evidence_key || ""}`),el("p",rule.detail),el("span",rule.code,"rule-code"));
    if(rule.validation_errors) box.append(el("pre",pretty(rule.validation_errors)));
    parent.append(box);
  }
}
function renderInspector() {
  const node = $("#inspectorContent"); clear(node);
  for (const button of document.querySelectorAll("[data-tab]")) button.setAttribute("aria-pressed",String(button.dataset.tab === inspectorTab));
  if (!currentRun) return;
  const stage = steps().find(s => s.id === selectedStep);
  $("#inspectorTitle").textContent = selectedEvidence ? `${selectedEvidence.record.record_id} · evidência` : stage?.name || "Execução completa";
  const list = selectedEvidence ? claimEvents(selectedEvidence.claimId) : selectedStep ? stepEvents(selectedStep) : events();
  const decisions = list.filter(e => e.kind === "verdict_decision");
  const rules = [...list.filter(e => e.kind === "check"),...decisions.flatMap(e => e.checks)];
  if (inspectorTab === "rules") {
    node.append(el("p","Python verifica referências e condições mínimas. Compatibilidade de contexto e qualidade da fonte continuam sendo julgamentos do modelo.","hint"));
    showRules(node,selectedEvidence ? rules.filter(r => !r.evidence_key || r.evidence_key === selectedEvidence.record.evidence_key) : rules); return;
  }
  if (selectedEvidence) {
    const key = selectedEvidence.record.evidence_key;
    if (inspectorTab === "input") {
      const bounded = list.find(e => e.kind === "assessment_input");
      jsonSection(node,"Trechos realmente enviados",bounded?.records.find(r => r.evidence_key === key) || "Entrada não capturada nesta execução.");
      jsonSection(node,"Truncamentos",bounded?.truncations.filter(t => t.evidence_key === key) || []);
    } else if(inspectorTab === "output") jsonSection(node,"Avaliação do modelo",list.find(e => e.kind === "assessment_proposal")?.output?.records.find(r => r.evidence_key === key) || selectedEvidence.record);
    else {
      jsonSection(node,"Por que foi recuperado",list.find(e => e.kind === "search_ranking")?.candidates.find(c => c.evidence_key === key) || "Detalhamento não capturado.");
      jsonSection(node,"Registro completo",selectedEvidence.record);
    }
    return;
  }
  if(inspectorTab === "input") {
    const inputs = list.filter(e => ["model_request","assessment_input"].includes(e.kind));
    if(!inputs.length) jsonSection(node,"Texto analisado",currentRun.input);
    for(const item of inputs) jsonSection(node,item.kind === "model_request" ? "Requisição exata ao modelo" : "Evidências após os limites de tamanho",item.request || item);
  } else if(inspectorTab === "output") {
    const outputs = list.filter(e => ["model_response","parsed_output","assessment_proposal","extraction"].includes(e.kind));
    if(!outputs.length) jsonSection(node,"Resultado",currentRun.result || "Aguardando resultado.");
    for(const item of outputs) {
      if(item.kind === "model_response") {
        jsonSection(node,"Metadados da resposta",{finish_reason:item.finish_reason,refusal:item.refusal,usage:item.usage,capture_truncated:item.capture_truncated});
        node.append(el("h3","Resposta original (pode ter sido rejeitada)"),el("pre",item.content || "Sem conteúdo textual."));
      } else jsonSection(node,item.kind === "parsed_output" ? "JSON validado" : "Proposta do modelo",item.output);
    }
  } else {
    if(stage) jsonSection(node,"Etapa",{name:stage.name,actor:stage.actor,status:stage.status,duration_ms:stage.duration_ms,claim_id:stage.claim_id,reason:stage.reason,error_message:stage.error_message});
    else {
      node.append(el("h3","Registro desta execução"),el("p",`${allClaims().length} afirmações · ${events().filter(e => e.kind === "model_request").length} chamadas ao modelo`),el("p","Clique em uma etapa para ver os dados específicos. Entrada e Saída preservam as requisições e respostas originais.","hint"));
      const snapshot = list.find(e => e.kind === "snapshot");
      if(snapshot) node.append(el("h3","Base consultada"),el("p",`${snapshot.record_count} registros · ${snapshot.cache_status === "STALE" ? "cache desatualizado" : "cache válido"}`),el("p",`Consulta: ${shortTime(snapshot.retrieved_at)}`,"hint"));
      for(const decision of decisions) {
        node.append(el("h3","Veredicto proposto e resultado"),el("p",`${decision.proposed || "Sem proposta"} → ${decision.accepted || "sem veredicto"}`));
        const blocked = decision.checks.filter(c => c.status === "failed");
        if(blocked.length) showRules(node,blocked);
        else node.append(el("p",decision.accepted ? "Nenhuma regra bloqueou a proposta. A conclusão continua provisória." : "O modelo não propôs um veredicto aplicável.","hint"));
      }
      const details = el("details"); details.append(el("summary","Identidade e configuração"));
      jsonSection(details,"Dados da execução",{job_id:currentRun.job_id,input_sha256:currentRun.input_sha256,parent_job_id:currentRun.parent_job_id,configuration:currentRun.audit?.configuration,coverage:currentRun.audit?.coverage});
      node.append(details);
    }
    if(stage) for(const item of list.filter(e => ["snapshot","search_ranking","team_routing","verdict_decision"].includes(e.kind))) jsonSection(node,({snapshot:"Base consultada",search_ranking:"Ranking da busca",team_routing:"Encaminhamento",verdict_decision:"Proposta e decisão de Python"})[item.kind],item);
    if(stage?.status === "failed") node.append(el("p","Esta etapa falhou. Confira a saída original e as regras para localizar a causa.","warning"));
    if(!stage && !events().length) node.append(el("p","Esta execução não contém etapas. Consulte Saída para o resultado salvo.","hint"));
  }
}

function renderCompareOptions() {
  const select = $("#compareRun"), previous = select.value; clear(select); select.append(el("option","Selecione outra execução")); select.firstChild.value = "";
  for(const summary of summaries) if(summary.job_id !== selectedId) {
    const option = el("option",`${summary.segment_id} · ${STATUS[summary.status] || summary.status} · ${summary.model || "modelo antigo"} · ${shortTime(summary.created_at)} · ${summary.job_id.slice(0,8)}`); option.value = summary.job_id; select.append(option);
  }
  select.value = previous;
}
function comparisonRows(left,right) {
  const rows = [
    ["Texto de entrada",left.input,right.input],
    ["Configuração",left.audit?.configuration || left.result?.model,right.audit?.configuration || right.result?.model],
    ["Código (SHA-256)",left.audit?.code_sha256,right.audit?.code_sha256],
    ["Snapshot da base",snapshotHash(left),snapshotHash(right)],
    ["Estado",left.status,right.status]
  ];
  const a = allClaims(left), b = allClaims(right);
  for(let i=0;i<Math.max(a.length,b.length);i++) {
    const compact = (c,run) => c ? {quote:c.quote,normalized_claim:c.normalized_claim,subjects:c.subjects,keywords:c.keywords,synonyms:c.synonyms,
      retrieval_status:c.retrieval_status,evidence:c.evidence.map(e => ({key:e.evidence_key,score:e.score,relevance:e.relevance,context_compatible:e.context_compatible,source_quality:e.source_quality,basis_quote:e.basis_quote})),
      proposed:verdictDecision(c.claim_id,run)?.proposed,accepted:c.suggested_verdict,explanation:c.explanation,assigned_team:c.assigned_team} : null;
    rows.push([`Afirmação ${i+1} (ordem de extração)`,compact(a[i],left),compact(b[i],right)]);
  }
  return rows;
}
async function compareRuns() {
  const otherId = $("#compareRun").value, run = currentRun;
  if(!otherId || !run) return;
  const button = $("#compare"); button.disabled = true;
  try {
    const other = await api(`/api/ai-debug/${otherId}`);
    if(currentRun?.job_id !== run.job_id) return;
    const box = $("#comparison"); clear(box);
    box.append(el("p",`Atual: ${run.job_id.slice(0,8)} · Comparada: ${otherId.slice(0,8)}`,"hint"));
    const sameInput = pretty(run.input) === pretty(other.input), sameSnapshot = snapshotHash(run) && snapshotHash(run) === snapshotHash(other);
    box.append(el("p",`${sameInput ? "Entrada idêntica." : "As entradas são diferentes."} ${sameSnapshot ? "Mesmo snapshot da base." : "Snapshots diferentes ou não registrados."} Afirmações são alinhadas pela ordem, não por equivalência semântica.`,sameInput && sameSnapshot ? "hint" : "warning"));
    const wrap = el("div",null,"table-scroll"), table = el("table");
    const header = el("tr"); for(const title of ["Campo","Execução atual","Execução comparada"]) header.append(el("th",title));
    const head = el("thead"); head.append(header); table.append(head); const body = el("tbody");
    for(const [label,a,b] of comparisonRows(run,other)) {
      const row = el("tr",null,pretty(a) === pretty(b) ? "" : "changed"); row.append(el("th",label),el("td",pretty(a) ?? "Não registrado","compare-value"),el("td",pretty(b) ?? "Não registrado","compare-value")); body.append(row);
    }
    table.append(body); wrap.append(table); box.append(wrap);
  } catch(error) { notice(error.message); } finally { button.disabled = false; }
}
async function saveReview() {
  const id = selectedId, value = {assessment:$("#reviewAssessment").value,notes:$("#reviewNotes").value};
  const button = $("#saveReview"); button.disabled = true;
  try {
    const review = await api(`/api/ai-debug/${id}/review`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
    if(selectedId === id) {
      currentRun.review = review;
      reviewDirty = $("#reviewAssessment").value !== value.assessment || $("#reviewNotes").value !== value.notes;
      if(!reviewDirty) reviewDrafts.delete(id);
      $("#reviewSaved").textContent = reviewDirty ? "Versão anterior salva; há alterações não salvas." : "Revisão salva localmente.";
    }
  } catch(error) { notice(error.message); } finally { button.disabled = false; }
}
async function copyPrompt() {
  try {
    const data = await api("/api/ai-debug/review-prompt");
    try { await navigator.clipboard.writeText(data.prompt); notice("Instrução de revisão copiada. Anexe também o JSON exportado ao enviar para outra IA."); }
    catch { const node = $("#inspectorContent"); clear(node); node.append(el("p","Selecione e copie a instrução abaixo.","hint"),el("pre",data.prompt)); notice("A cópia automática não está disponível neste navegador."); }
  } catch(error) { notice(error.message); }
}
async function poll() {
  if(pollBusy) return;
  pollBusy = true;
  try {
    const data = await api("/api/ai-debug?limit=200"); summaries = data.jobs;
    $("#runCount").textContent = `${summaries.length} / ${data.total}`;
    $("#historyLimit").textContent = data.truncated ? "Exibindo as 200 execuções mais recentes. Links diretos continuam abrindo execuções anteriores." : "Histórico salvo nesta sessão de gravação.";
    renderRuns();
    if(selectedId) await updateSelected();
    else if(summaries.length) await selectRun(summaries[0].job_id);
    $("#connection").textContent = data.enabled ? "Atualização ao vivo" : "Histórico local · IA desabilitada";
    $("#connectionDot").className = "connection-dot online";
  } catch(error) {
    $("#connection").textContent = "Sem conexão · tentando novamente"; $("#connectionDot").className = "connection-dot offline";
    notice(error.message);
  } finally { pollBusy = false; }
}
function init() {
  const initial = new URL(location.href).searchParams.get("run");
  if(initial && /^[a-f0-9]{32}$/.test(initial)) selectedId = initial;
  $("#searchRuns").addEventListener("input",renderRuns); $("#filterRuns").addEventListener("change",renderRuns);
  $("#refresh").addEventListener("click",poll); $("#compare").addEventListener("click",compareRuns);
  $("#copyPrompt").addEventListener("click",copyPrompt); $("#saveReview").addEventListener("click",saveReview);
  for(const selector of ["#reviewAssessment","#reviewNotes"]) $(selector).addEventListener("input",() => {
    reviewDirty = true;
    reviewDrafts.set(selectedId,{assessment:$("#reviewAssessment").value,notes:$("#reviewNotes").value});
    $("#reviewSaved").textContent = "Alterações não salvas.";
  });
  for(const button of document.querySelectorAll("[data-tab]")) button.addEventListener("click",() => { inspectorTab = button.dataset.tab; renderInspector(); });
  window.addEventListener("beforeunload",event => { if(reviewDirty || reviewDrafts.size) { event.preventDefault(); event.returnValue = ""; } });
  poll(); setInterval(poll,1500);
}
init();
