# AI audit dashboard

Start the operator server as usual and open http://127.0.0.1:8765/debug, or use
**Inspecionar IA** in the toolbar. After starting **Analisar com IA**, its result
panel also links directly to that run. No extra process, dependency or setting is
required. The dashboard uses the same local-only HTTP server and session directory.
It does not send messages, publish findings, write to Sheets or start inference.

## Inspecting a run

The left column lists the latest 200 saved/live runs, with text search and filters
for running jobs, failures, withheld verdicts and no matches. Older runs remain
accessible using their direct `/debug?run=<job_id>` links. The selected run retains
its immutable input text and speaker. A warning appears if a matching current
operator block has since changed. Direct API inputs without a matching block have
unknown freshness. The timeline updates every 1.5 seconds, including planned,
running, completed, failed, skipped and interrupted work.

Click a step to inspect:

- **Resumo:** stage metadata, search tokens, score contributions, selected and
  excluded ranked candidates, cache state, routing and verdict decisions.
- **Entrada:** exact model messages, response schema and generation settings;
  bounded evidence payloads with explicit per-field truncations.
- **Saída:** original model response, finish reason, available token usage,
  parsed JSON and model proposals, including responses subsequently rejected.
- **Regras:** exact-quote checks, schema failures without echoed validation inputs,
  evidence-reference checks and individual verdict gates.

The evidence grid separates model relevance/context/source ratings from Python
quote checks and verdict rules. Click a record ID for its ranking and the excerpts
actually sent; expand its evidence section for full fields and separate row/source
links. These links are not fetched by the pipeline. Search scores are heuristic,
not probabilities. Passing deterministic gates does not prove a factual verdict.

**Comparar execuções** compares saved inputs, configuration, code hashes, snapshot
hashes, extracted propositions, retrieval rankings, evidence assessments, proposals,
accepted verdicts and routing. Changed rows are highlighted. Claims are aligned by
extraction order; this is not semantic claim matching or a controlled model replay.
Different input/snapshot warnings identify comparisons with changed conditions.

**Sua revisão** saves an assessment and up to 8,000 characters of notes in a
separate review file. Unsaved drafts survive switching between runs while the page
stays open. Save them before closing/reloading the page; the browser warns about
unsaved drafts. Annotations never modify original model responses or verdicts.

## Exporting for another AI

**Exportar auditoria JSON** downloads a versioned bundle containing the exact input,
execution configuration, source-code hashes, events, model calls, retrieved
evidence, validation checks, proposed/accepted results, review annotations and
coverage limitations. **JSON com base completa** additionally includes the frozen
validated workbook records used for this run, when captured successfully. The
export says explicitly whether that snapshot is included. Snapshot hashes include
record metadata, timestamps and cache state, so a new read can have a different
hash even if cell content stayed the same.

**Copiar instrução de revisão** provides a prompt for reviewing the attached JSON.
Export and upload are manual: this app never sends audit bundles to another AI.
The normal bundle supports assessment review; without the complete workbook it
cannot establish whether retrieval missed a better record. Factual verification
may still require a human to inspect external documents.

Only allowlisted configuration is recorded; environment variables, API keys,
credential files, authentication headers and SDK exception payloads are excluded.
Audit files do contain transcript text, workbook cells and model output. Original
textual responses are captured up to 200,000 characters, with an explicit truncation
flag. Hidden reasoning is not captured. Ranking explanations include the first 200
eligible records and an omitted count; the entire validated workbook is available
in the optional full export. In-progress exports are snapshots of incomplete runs.

## Persistence and API

Files under `data/<session>/ai_evidence/` are git-ignored with the session:

| File | Purpose |
| --- | --- |
| `<job_id>.json` | Initial job snapshot, replaced atomically with final result and audit |
| `<job_id>.events.jsonl` | Append-only events, flushed by closing each write |
| `<job_id>.snapshot.json` | Full validated evidence snapshot, saved before assessment |
| `<job_id>.summary.json` | Compact history index for polling without loading model payloads |
| `<job_id>.review.json` | Separately saved human annotation |

Finished runs restore from disk. A saved processing run absent from the current
server's live jobs is reported as `INTERRUPTED`; its valid event lines survive a
partial final line. Interrupted work is not automatically retried. Older analysis
files remain readable, with an explicit notice that original steps were not captured.
Disk-write failures are exposed in the run warning while results remain in memory.
Retention is the session's responsibility: the dashboard does not delete history.
As before, run one operator server per session directory.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/ai-debug?limit=100` | Compact run history; limit clamped to 1–200 |
| `GET /api/ai-debug/<job_id>?after=0` | Run details and events after a sequence number |
| `GET /api/ai-debug/<job_id>/export` | Standard audit bundle |
| `GET /api/ai-debug/<job_id>/export?full=1` | Bundle with the saved complete evidence snapshot |
| `GET /api/ai-debug/review-prompt` | Review instruction text |
| `POST /api/ai-debug/<job_id>/review` | Save `{assessment, notes}` separately |

Review assessments are `unreviewed`, `looks_correct`, `needs_investigation`, or
`error_found`. Job IDs are validated as 32 hexadecimal characters before any
file access. Existing host checks apply to assets, history, exports and annotations.
The ordinary `/api/ai-evidence/<job_id>` response omits the large audit object to
keep operator polling small; its input/result/status contract is preserved.

## Verification

```bash
python -m unittest discover -v
node test_operator_ui.js
node test_debug_ui.js
```

The tests use mocked model and Sheets responses. They cover invalid JSON,
invented IDs/quotes, stale evidence, no claims/no match, timeouts, input changes,
bounded cell excerpts, concurrent trace isolation, persisted history, interrupted
event recovery, safe rendering, polling races, review drafts, comparison and exports.
They do not measure a real model's factual accuracy or invoke paid inference.
