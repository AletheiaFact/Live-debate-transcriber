// Run with: node test_operator_ui.js. Uses no browser or external services.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(`${__dirname}/static/index.html`, "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function element(tag = "div") {
  const listeners = new Map();
  return {tagName: tag.toUpperCase(), children: [], dataset: {}, classList: {add() {}},
    addEventListener(type, listener, options) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push({listener, once: options?.once});
    },
    dispatch(type, event = {}) {
      const handlers = listeners.get(type) || [];
      listeners.set(type, handlers.filter(x => !x.once));
      for (const x of handlers) x.listener(event);
    },
    showModal() {this.open = true;},
    close(value) {this.open = false; this.returnValue = value; this.dispatch("close");},
    setAttribute(name, value) {this[name] = value;},
    append(...children) {this.children.push(...children);}};
}
const elements = new Map();
const select = selector => {
  if (!elements.has(selector)) elements.set(selector, element());
  return elements.get(selector);
};
const textOf = el => typeof el === "string" ? el : (el.textContent || "") + el.children.map(textOf).join("");
const context = vm.createContext({
  document: {querySelector: select, addEventListener() {},
    createElement: element, createDocumentFragment: element},
  window: {addEventListener() {}},
});
vm.runInContext(script.replace(/\npoll\(\);\s*$/, ""), context);
const run = code => vm.runInContext(code, context);
run('settings = {debate_start: "2026-10-22T23:30:00-04:00", show_minutage: true}');
assert.equal(run('blockTime({start: "2026-10-23T00:01:00-04:00"})'), "00:31:00");
assert.equal(run('blockTime({start: "2026-10-22T23:29:00-04:00"})'), "−00:01:00");
assert.equal(run('blockTime({start: "2026-10-24T00:01:00-04:00"})'), "24:31:00");
run('labels = {a: "1"}');
assert.equal(run('lineFor({id: "a", start: "2026-10-23T00:01:00-04:00", text: "Claim"})'),
  "[00:31:00] Lula: Claim");
run('settings.show_minutage = false');
assert.equal(run('lineFor({id: "a", start: "2026-10-23T00:01:00-04:00", text: "Claim"})'),
  "[00:01:00] Lula: Claim");

async function deliveryChecks() {
  run(`
    platform = {target: "sheets", configured: true};
    blocks = [{id: "a", text: "Claim", status: "ok", final: true, start: "2026-10-23T00:01:00-04:00"},
              {id: "b", text: "Next", status: "ok", final: true}];
    labels = {a: "1", b: "1"};
    render = () => {};
    toast = () => {};
  `);
  let resolveSend;
  let calls = 0;
  context.fetch = () => {calls++; return new Promise(resolve => {resolveSend = resolve;});};
  const sending = run('sendBlock("a")');
  assert.equal(run('sendPending.has("a")'), true);
  assert.equal(run('isLocked("a")'), true);
  await run('sendBlock("a")');
  assert.equal(calls, 1, "double click must not create a second POST");
  resolveSend({ok: true, json: async () => ({status: "queued"})});
  await sending;
  assert.equal(run('sendPending.has("a")'), false);
  assert.equal(run('isLocked("a")'), true);

  run('sendByBlock.get("a").status = "sent"');
  await run('setLabel("a", "2")');
  run('openEditor("a")');
  await run('saveEdits({a: "changed"}, "saved")');
  await run('changeMerge(blocks[0])');
  assert.equal(calls, 1, "locked editing actions must not issue requests");
}

async function main() {
  await deliveryChecks();
  assert.equal(run('labels.a'), "1");
  assert.equal(run('editingId'), null);
  assert.equal(run('isLocked("a")'), true);
  const block = run('buildBlock(blocks[0], "1", "")');
  const buttons = block.children[0].children.filter(x => x && ["Editar", "Combinar ↓", "Enviar"].includes(x.textContent));
  assert.equal(buttons.length, 3);
  assert.ok(buttons.every(x => x.disabled), "sent blocks keep all editing/sending buttons disabled");
  assert.ok(block.children[0].children.some(x => x?.textContent === "enviado à planilha"));
  run('sendByBlock.get("a").status = "failed"');
  assert.equal(run('isLocked("a")'), false);
  const failed = run('buildBlock(blocks[0], "1", "")');
  assert.equal(failed.children[0].children.find(x => x?.textContent === "Tentar novamente").disabled, false);
  let resolvePoll;
  context.fetch = () => new Promise(resolve => {resolvePoll = resolve;});
  const stalePoll = run('refreshState()');
  run('stateRevision++'); // a send was acknowledged while this older poll was in flight
  resolvePoll({ok: true, json: async () => ({blocks: []})});
  await stalePoll;
  assert.equal(run('blocks.length'), 2, "a stale poll must not erase the local send acknowledgement");

  run(`
    aiEnabled = true;
    aiJobs.set("a", {job_id: "abc", status: "COMPLETED", inputText: "Claim", inputSpeaker: "Lula", result: {
      status: "COMPLETED", errors: [], provider: "ollama", model: "test", analyzed_at: "now",
      claims: [{quote: "Claim", normalized_claim: "Claim", retrieval_status: "MATCH_FOUND",
        suggested_verdict: null, explanation: "Revisão humana", assigned_team: "L2", team_candidates: ["L2"], errors: [],
        evidence: [{record_id: "LU106", sheet_name: "Lula", relevance: "DIRECT_MATCH", record_type: "Declaração",
          team: "L2", existing_label: "Confiável", summary: "Registro relacionado", retrieved_at: "now", cache_status: "FRESH",
          spreadsheet_row_url: "https://docs.google.com/spreadsheets/d/test/edit#gid=1277645709&range=A176:Y176",
          source_urls: ["https://example.org/source"], fields: {ID: "LU106"}}]}]},
      delivery_preview: {userEnteredValue: {stringValue: "Frase: Claim\\nAbrir registro original"}, textFormatRuns: [
        {startIndex: 0, format: {bold: true}}, {startIndex: 7, format: {}},
        {startIndex: 13, format: {link: {uri: "https://example.org/row"}}}]}});
  `);
  const evidenceBox = run('buildEvidence(blocks[0])');
  assert.equal(evidenceBox.tagName, "DETAILS");
  assert.equal(evidenceBox.open, false);
  evidenceBox.open = true;
  evidenceBox.dispatch("toggle");
  assert.equal(run('buildEvidence(blocks[0]).open'), true, "poll-driven rebuilds preserve expanded state");
  const preview = evidenceBox.children.find(x => x.className === "ai-preview");
  assert.equal(textOf(preview), run('aiJobs.get("a").delivery_preview.userEnteredValue.stringValue'));
  assert.equal(preview.children[0].tagName, "STRONG");
  const rowLink = preview.children.find(x => x.tagName === "A");
  assert.equal(rowLink.href, "https://example.org/row");
  assert.equal(rowLink.rel, "noopener noreferrer");
  assert.ok(!textOf(evidenceBox).includes("Confiável"), "full raw evidence stays in Debug");
  assert.equal(run('(() => {const el = document.createElement("div"); evidenceLink(el, "javascript:alert(1)", "unsafe"); return el.children.length;})()'), 0);
  run('aiJobs.get("a").inputText = "old"');
  assert.ok(!run('buildEvidence(blocks[0])').children.some(x => x.className === "ai-preview"), "outdated content must never look ready to send");
  run('aiJobs.get("a").inputText = "Claim"');
  run('aiJobs.get("a").status = "PROCESSING"');
  const processing = run('buildBlock(blocks[0], "1", "")');
  assert.equal(processing.children[0].children.find(x => x.textContent === "Analisando…").disabled, true);

  run('sendByBlock.clear()');
  run('aiEnabled = false');
  context.fetch = async () => {throw new Error("offline");};
  await run('sendBlock("a")');
  assert.equal(run('sendUnconfirmed.has("a")'), true);
  assert.equal(run('isLocked("a")'), true, "lost POST and state responses must keep the block locked");
  assert.equal(run('JSON.stringify([0, 24.9, 25, 60, 60.1, 100].map(checkBand))'), JSON.stringify(["low", "low", "mid", "mid", "high", "high"]));
  run('checkability = {a: {percentage: 72.4, method: "max_of_sources", model: "jev-1.13.0", rubric_version: 1, stale: false}}');
  const scored = run('buildBlock(blocks[0], "1", "")').children[0].children.find(x => x?.className?.startsWith("chk"));
  assert.equal(scored.textContent, "72%");
  assert.equal(scored.className, "chk chk-high");
  run('checkability.a.stale = true');
  assert.match(run('buildBlock(blocks[0], "1", "")').children[0].children.find(x => x?.className?.startsWith("chk")).className, / stale$/);
  run('checkability = {}');
  assert.ok(!run('buildBlock(blocks[0], "1", "")').children[0].children.some(x => x?.className?.startsWith("chk")));
  console.log("UI checkability badge checks passed.");
  const themeBadgeOf = () => run('buildBlock(blocks[0], "1", "")').children[0].children.find(x => x?.className?.startsWith("theme"));
  run(`themes = {a: {theme: "Saúde, educação e políticas sociais", family: "Economia e políticas sociais", method: "jev_direct",
    model: "jev-1.13.0", theme_rubric_version: 1, stale: false,
    probabilities: {"Economia e finanças públicas": 0.1, "Saúde, educação e políticas sociais": 0.8, "Sem tema": 0.1}}}`);
  assert.equal(themeBadgeOf().textContent, "Saúde/Educação");
  assert.equal(themeBadgeOf().className, "theme theme-social");
  assert.match(themeBadgeOf().title, /Saúde, educação e políticas sociais 80% · Economia e finanças públicas 10%/);
  run('themes.a.probabilities = {"Economia e finanças públicas": 0.45, "Saúde, educação e políticas sociais": 0.5, "Sem tema": 0.05}');
  assert.equal(themeBadgeOf().textContent, "Saúde/Educação ?");
  run('Object.assign(themes.a, {theme: "Sem tema", family: null, stale: true})');
  assert.equal(themeBadgeOf().className, "theme theme-none stale");
  run('themes = {}');
  assert.equal(themeBadgeOf(), undefined);
  console.log("UI theme badge checks passed.");
  await aiSendChecks();
  console.log("UI timestamp, send locks, retry, AI processing, row/source links and safe URL checks passed.");
}

async function aiSendChecks() {
  run('sendByBlock.clear(); sendUnconfirmed.clear(); aiJobs.clear(); aiEnabled = true; allowWithoutAI = false;');
  let calls = [];
  context.fetch = async (url, options) => {
    calls.push(JSON.parse(options.body));
    return {ok: true, json: async () => ({status: "queued", ai_cell: null})};
  };
  let sending = run('sendBlock("a")');
  assert.equal(select("#aiSendConfirm").open, true);
  assert.equal(calls.length, 0);
  await run('sendBlock("a")');
  select("#aiSendConfirm").close("cancel");
  await sending;
  assert.equal(calls.length, 0, "cancel and a duplicate send must not post");

  sending = run('sendBlock("a")');
  select("#aiSendConfirm").close("send");
  await sending;
  assert.equal(calls[0].allowWithoutAI, true);
  assert.deepEqual(calls[0].aiJobIds, {});

  run('sendByBlock.clear(); allowWithoutAI = true; aiJobs.set("a", {job_id: "running", status: "PROCESSING"});');
  await run('sendBlock("a")');
  assert.equal(calls[1].allowWithoutAI, true);
  assert.equal(calls[1].aiJobIds.a, "running", "server can include a job that finishes before enqueue");
  assert.equal(select("#aiSendConfirm").open, false, "the bypass checkbox suppresses repeated prompts");

  run(`sendByBlock.clear(); allowWithoutAI = false; aiJobs.set("a", {
    job_id: "ready", status: "COMPLETED", inputText: "Claim", inputSpeaker: "Lula",
    delivery_preview: {userEnteredValue: {stringValue: "Same summary"}, textFormatRuns: [{startIndex: 0, format: {bold: true}}]}
  });`);
  context.fetch = async (url, options) => {
    calls.push(JSON.parse(options.body));
    return {ok: true, json: async () => ({status: "queued", ai_cell: run('aiJobs.get("a").delivery_preview')})};
  };
  await run('sendBlock("a")');
  assert.equal(calls[2].aiJobIds.a, "ready");
  assert.equal(calls[2].allowWithoutAI, false);
  assert.equal(select("#aiSendConfirm").open, false, "usable results send without prompting");
  run('aiJobs.get("a").delivery_preview.userEnteredValue.stringValue = "New inference"');
  // Real HTTP responses are JSON copies; use a separate saved cell for this assertion.
  run('sendByBlock.get("a").ai_cell = {userEnteredValue: {stringValue: "Frozen summary"}, textFormatRuns: [{startIndex: 0, format: {}}]}');
  assert.ok(textOf(run('buildEvidence(blocks[0])')).includes("Frozen summary"));
  assert.ok(!textOf(run('buildEvidence(blocks[0])')).includes("New inference"));

  run('sendByBlock.clear(); aiJobs.get("a").status = "ERROR";');
  let analyzed = null;
  context.captureAnalyze = (id, refresh) => {analyzed = {id, refresh};};
  run('analyzeBlock = (id, refresh) => captureAnalyze(id, refresh)');
  sending = run('sendBlock("a")');
  select("#aiSendConfirm").close("analyze");
  await sending;
  assert.deepEqual(analyzed, {id: "a", refresh: true});
  assert.equal(calls.length, 3, "Analyze first must not send the block");
  select("#allowWithoutAI").dispatch("change", {target: {checked: true}});
  assert.equal(run('allowWithoutAI'), true);
  assert.equal(select("#aiBypassWarning").hidden, false);
  console.log("AI preview parity, collapse state, confirmation, bypass and frozen snapshot checks passed.");
}
main().catch(error => {console.error(error); process.exitCode = 1;});
