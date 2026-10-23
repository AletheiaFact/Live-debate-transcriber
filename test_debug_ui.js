// Offline UI behavior checks. Run: node test_debug_ui.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.value = ""; this.handlers = {}; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  set innerHTML(_) { throw new Error("Untrusted audit content must never use innerHTML"); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ""; this.children = [...nodes]; }
  setAttribute(name,value) { this.attributes[name] = value; }
  addEventListener(name,callback) { this.handlers[name] = callback; }
  get firstChild() { return this.children[0]; }
}
const nodes = new Map(), tabs = ["overview","input","output","rules"].map(tab => { const node = new Element("button"); node.dataset.tab = tab; return node; });
const node = selector => { if(!nodes.has(selector)) nodes.set(selector,new Element()); return nodes.get(selector); };
const context = vm.createContext({
  document:{querySelector:node,querySelectorAll:() => tabs,createElement:tag => new Element(tag),createTextNode:text => {const n = new Element("text"); n.textContent = text; return n;}},
  URL, location:{href:"http://127.0.0.1/debug"}, history:{replaceState(){}}, navigator:{clipboard:{writeText:async () => {}}}, window:{addEventListener(){}},
  setInterval(){}, console,
});
vm.runInContext(fs.readFileSync("static/debug.js","utf8").replace(/\ninit\(\);\s*$/,""),context);
const run = code => vm.runInContext(code,context);
const clone = object => JSON.parse(JSON.stringify(object));

function fixture(id = "a".repeat(32)) {
  return {job_id:id,segment_id:"s42",status:"COMPLETED",created_at:"2026-10-23T16:00:00Z",input:{speaker:"Lula",transcription:"A população aumentou em 2025."},input_sha256:"same-input",review:{assessment:"unreviewed",notes:""},
    audit:{configuration:{model:"local-test",provider:"ollama"},last_seq:10,events:[
      {seq:1,kind:"step_started",child_step_id:"root",name:"Análise do bloco",actor:"Python"},
      {seq:2,kind:"step_started",child_step_id:"assess",parent_step_id:"root",step_id:"root",name:"Avaliar evidências e aplicar regras",claim_id:"c1",actor:"Model + Python"},
      {seq:3,kind:"model_request",step_id:"assess",request:{messages:[{content:"Original input"}]}},
      {seq:4,kind:"model_response",step_id:"assess",content:"<script>globalThis.pwned = true</script>",finish_reason:"stop",usage:{total_tokens:42}},
      {seq:5,kind:"verdict_decision",step_id:"assess",proposed:"VERDADEIRO",accepted:null,withheld:true,checks:[{code:"fresh_cache",status:"failed",evidence_key:"sheet:r1",detail:"Cache desatualizado."}]},
      {seq:6,kind:"step_finished",step_id:"assess",status:"completed",duration_ms:350},
      {seq:7,kind:"snapshot",step_id:"root",sha256:"snapshot-1",record_count:1,cache_status:"STALE"},
      {seq:8,kind:"step_finished",step_id:"root",status:"completed",duration_ms:500},
      {seq:9,kind:"check",step_id:"assess",code:"exact_documentary_quote",evidence_key:"sheet:r1",status:"passed",detail:"Exato."},
      {seq:10,kind:"run_finished",status:"COMPLETED"}
    ]},result:{claims:[{claim_id:"c1",quote:"A população aumentou em 2025.",normalized_claim:"A população aumentou em 2025.",retrieval_status:"REVIEW_REQUIRED",suggested_verdict:null,team_candidates:[],explanation:"Revisão humana necessária.",
      evidence:[{evidence_key:"sheet:r1",record_id:"r1",sheet_name:"Base",score:10,relevance:"DIRECT_MATCH",context_compatible:true,source_quality:"RELIABLE",cache_status:"STALE",basis_quote:"Trecho documental",fields:{ID:"r1"},source_urls:["https://example.org/doc"],primary_source_urls:[],spreadsheet_row_url:"https://example.org/row"}]}]}};
}
context.sample = fixture();
node("#filterRuns").value = "all";
run("currentRun = sample; selectedId = sample.job_id; renderRun()");
assert.match(node("#claims").textContent,/Proposta retida: VERDADEIRO/);
assert.match(node("#claims").textContent,/Cache desatualizado/);
assert.equal(node("#exportRun").href,`/api/ai-debug/${"a".repeat(32)}/export`);
assert.match(node("#inspectorContent").textContent,/Cache desatualizado/);
run('selectedStep = "assess"; inspectorTab = "output"; renderInspector()');
assert.match(node("#inspectorContent").textContent,/<script>globalThis.pwned = true<\/script>/);
assert.equal(run("typeof pwned"),"undefined");
run('inspectorTab = "rules"; renderInspector()');
assert.match(node("#inspectorContent").textContent,/Falhou/);
run('selectedEvidence = {claimId:"c1",record:sample.result.claims[0].evidence[0]}; renderTranscript()');
assert.equal(node("#transcript").children[1].tag,"mark");
run('selectedEvidence = null');
const links = new Element(); context.linkParent = links;
run('safeLink(linkParent,"javascript:alert(1)","bad"); safeLink(linkParent,"https://user:pass@example.org","bad"); safeLink(linkParent,"https://example.org/doc","good")');
assert.equal(links.children.length,1);
assert.equal(links.children[0].rel,"noopener noreferrer");

async function checks() {
  context.fetch = async path => ({ok:true,json:async () => {
    assert.match(path,/after=(10|11)/);
    const data = fixture(); data.audit.last_seq = 11;
    data.audit.events = [{seq:11,kind:"check",step_id:"assess",code:"new",status:"passed",detail:"New event"}];
    return data;
  }});
  await run("updateSelected()");
  assert.equal(run("events().length"),11,"incremental polling must retain earlier events");
  await run("updateSelected()"); // same seq can be returned after a request retry
  assert.equal(run("events().length"),11,"sequence IDs must not be duplicated");

  let release;
  context.fetch = () => new Promise(resolve => { release = resolve; });
  const oldPoll = run("updateSelected()");
  run('selectionVersion++; selectedId = "b".repeat(32)');
  release({ok:true,json:async () => fixture()}); await oldPoll;
  assert.equal(run("selectedId"),"b".repeat(32),"stale responses must not change the selection");

  const legacy = fixture("c".repeat(32)); delete legacy.audit; context.legacy = legacy;
  run("currentRun = legacy; selectedId = legacy.job_id; selectedStep = null; renderRun()");
  assert.match(node("#runWarning").textContent,/anterior ao dashboard/);
  assert.match(node("#claims").textContent,/A população/);

  context.fetch = async path => ({ok:true,json:async () => path.includes("b".repeat(32)) ? fixture("b".repeat(32)) : fixture()});
  run('currentRun = sample; selectedId = sample.job_id; reviewDirty = true');
  node("#reviewNotes").value = "Unsaved draft";
  node("#reviewAssessment").value = "needs_investigation";
  await run('selectRun("b".repeat(32))');
  await run('selectRun("a".repeat(32))');
  assert.equal(node("#reviewNotes").value,"Unsaved draft","switching runs must preserve review drafts");

  const other = fixture("b".repeat(32)); other.result.claims[0].normalized_claim = "A população dobrou em 2025.";
  context.other = other;
  assert.match(run("JSON.stringify(comparisonRows(sample,other))"),/dobrou/);
  node("#compareRun").value = other.job_id;
  context.fetch = async () => ({ok:true,json:async () => clone(other)});
  await run("compareRuns()");
  assert.match(node("#comparison").textContent,/Entrada idêntica/);
  assert.match(node("#comparison").textContent,/dobrou/);

  const active = fixture(); active.status = "PROCESSING";
  active.audit.events = [{seq:1,kind:"step_planned",name:"Buscar evidências",actor:"Search",claim_id:"c2"}];
  context.active = active;
  assert.equal(run("steps(active)[0].status"),"pending");
  active.status = "INTERRUPTED";
  assert.equal(run("steps(active)[0].status"),"not_evaluated");
  console.log("Debug UI checks passed: raw outputs, gates, safe rendering, legacy runs, incremental polling, selection races, review drafts, comparison and pending steps.");
}
checks().catch(error => {console.error(error);process.exitCode=1;});
