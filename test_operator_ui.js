// Run with: node test_operator_ui.js. Uses no browser or external services.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(`${__dirname}/static/index.html`, "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function element() {
  return {children: [], dataset: {}, classList: {add() {}}, addEventListener() {},
    append(...children) {this.children.push(...children);}};
}
const context = vm.createContext({
  document: {querySelector: () => ({addEventListener() {}}), addEventListener() {},
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

  run('sendByBlock.clear()');
  context.fetch = async () => {throw new Error("offline");};
  await run('sendBlock("a")');
  assert.equal(run('sendUnconfirmed.has("a")'), true);
  assert.equal(run('isLocked("a")'), true, "lost POST and state responses must keep the block locked");
  console.log("UI timestamp, duplicate-click, sent-lock and retry checks passed.");
}
main().catch(error => {console.error(error); process.exitCode = 1;});
