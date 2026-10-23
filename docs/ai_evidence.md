# AI-assisted evidence retrieval

The optional **Analisar com IA** button analyzes a finalized transcription block,
including operator corrections and merges. Results appear below that block with
quoted claims, applicability, a provisional verdict when justified, team routing,
record IDs, **Abrir linha original**, and separate documentary-source links.
An edited block or changed speaker displays a warning beside its older result.
**Atualizar base e reanalisar** explicitly refreshes the evidence cache.

## Setup

Install `requirements.txt` into the environment running `server.py`. This repo's
uv environment can use `uv pip install --python .venv/Scripts/python.exe -r requirements.txt`.
Add these options to `.env` without replacing the existing transcription settings:

```dotenv
AI_EVIDENCE_ENABLED=true
AI_EVIDENCE_PROVIDER=ollama
AI_EVIDENCE_OLLAMA_BASE_URL=http://192.168.0.180:11434/v1
AI_EVIDENCE_OLLAMA_MODEL=qwen3.5:4b
AI_EVIDENCE_OLLAMA_REASONING_EFFORT=none
AI_EVIDENCE_SPREADSHEET_ID=1rY0NKhb_n2meBSTfAYFXEsUoJaxZku31XZMs-qVNc3k
```

`qwen3.5:4b` was present on the remote server when checked on 2026-10-23.
Model availability and latency depend on that machine. Run the diagnostic:

```bash
python ai_evidence.py --check
```

It lists available models and validates access, current headers, unique record IDs,
and all four evidence tabs without modifying any spreadsheet. The Google client
reuses `GOOGLE_APPLICATION_CREDENTIALS` but requests only
`spreadsheets.readonly`. Share the evidence workbook with the key's `client_email`
as **Viewer**. This is independent of Editor access for the live-transcript
destination. Never change `SPREADSHEET_ID` or `GOOGLE_SHEETS_TAB` for AI retrieval.

To switch to production, change configuration and restart the operator server:

```dotenv
AI_EVIDENCE_PROVIDER=openai
AI_EVIDENCE_OPENAI_BASE_URL=https://api.openai.com/v1
AI_EVIDENCE_OPENAI_MODEL=gpt-4.1-mini
OPENAI_API_KEY=your-key
```

The client verifies the configured model through `/v1/models`, uses the OpenAI
Python SDK for both providers, disables automatic SDK retries and sets a 90-second
timeout per request. Override `AI_EVIDENCE_TIMEOUT_SECONDS` (1–300). A segment can
require extraction plus up to 12 assessment calls, so its total duration can be
longer. Two jobs can run concurrently. The UI and recorder remain independent.

`AI_EVIDENCE_JSON_MODE=json_schema` is the default. For a provider/model that does
not support JSON Schema, explicitly select `json_object`; both modes always use
strict Python validation. Invalid JSON, refusals, truncated outputs, invented
record references, and altered exact quotes are rejected. Ollama uses
`reasoning_effort=none` for latency; configure
`AI_EVIDENCE_OLLAMA_REASONING_EFFORT=` to omit the option for older servers.
Generation defaults to temperature 0 for repeatability. Set
`AI_EVIDENCE_TEMPERATURE=` to omit that parameter for models that do not support it.
The remote Ollama server must listen on its LAN interface and its firewall must
allow the client. Keep that unauthenticated API off the public internet.

## Retrieval and editorial behavior

`evidence_search.py` defines four independent column maps by sheet ID. Titles are
read from workbook metadata, so renamed tabs still resolve. Row 1 must match the
verified current header schema; changed layouts fail rather than silently mixing
columns. Heading rows without record IDs are skipped. Complete rows are retained.

All four tabs are always searched. Only F/G/L/T and secondary N in tracking tabs,
C/D and secondary H in narratives, and E/F/I and secondary J in plans participate
in lexical retrieval. Accent/case folding, stop words including candidate names,
at least two distinctive overlapping tokens, inverse document frequency,
field-length normalization and multiword bonuses limit broad matches. Search
scores are heuristic, not probabilities. Candidate metadata can break close plan
ranking ties but cannot generate a match. Narratives' circulating group never
determines the claim's subject.

The default maximum is 10 candidates (configurable 1–20). The cache lasts 300
seconds. An expired snapshot is replaced atomically after all tabs validate.
Refresh failure retains a clearly marked stale snapshot, retries after a short
backoff, suppresses factual verdicts and returns a partial result. Without a valid
snapshot, search fails explicitly. Default read bound is 10,000 rows per tab;
`AI_EVIDENCE_MAX_ROWS_PER_TAB` can raise it up to 50,000. Overflow is an error.

Optional JSON settings in `.env`:

```dotenv
AI_EVIDENCE_MAX_CANDIDATES=10
AI_EVIDENCE_CACHE_TTL_SECONDS=300
AI_EVIDENCE_WEIGHTS={"tracking":{"L":5,"G":4,"F":3,"T":3,"N":1},"narratives":{"C":5,"D":4,"H":1},"plans":{"I":5,"F":4,"E":3,"J":1}}
AI_EVIDENCE_TOPIC_TEAMS={"saúde":["F2","L1"]}
```

Weights cannot introduce other searchable columns. Topic routing is opt-in and
only applies when retrieval returns no records. Teams in applicable records take
precedence; conflicting codes yield `assigned_team=null` and `team_candidates`.
The original full team strings remain on each evidence record.

There is **no automatic mapping** from legacy labels (including Confiável,
Enganoso, Exagerado and Não é fato) to the new four-category protocol. The LLM
assesses every candidate as DIRECT_MATCH, PARTIAL_MATCH, BACKGROUND_ONLY or
NOT_RELEVANT, checking dates, entities, geography, numbers, definitions, context,
source quality and recency. Python only permits a factual verdict with direct,
context-compatible, reliable assessments, source URLs, and an exact documentary
basis quoted from **O que os dados mostram**, independently of classification
fields. Anticipated/preliminary records, promises, proposals and stale records
cannot justify a verdict. Unsupported suggestions are withheld with an explanation.

External documents are **not downloaded or independently authenticated** in v1.
Assessment uses the workbook's summaries and source attributions; the operator
must open documentary sources. `INSUFICIENTE` remains in the protocol/schema but
is withheld in v1 because this service does not conduct a documented investigation
of all pertinent sources. No database match is `NO_MATCH` with a null verdict,
never a factual finding. All results remain `PENDING_HUMAN_REVIEW`. Analysis never
approves, publishes, sends, or changes the live-control tab or any evidence row.

Record IDs plus sheet IDs are durable identities. URLs are constructed by Python
from current snapshot row positions, using A:Y for tracking and A:S for the other
tabs. Explicit refresh resolves moved records. Links from a cached snapshot can
be outdated after someone rearranges the workbook; the UI shows retrieval times.

## API and schemas

`schemas.py` is the validation contract. Export the public JSON Schema with:

```bash
python -c "import json; from schemas import AnalysisResult; print(json.dumps(AnalysisResult.model_json_schema(), indent=2))"
```

The localhost HTTP server exposes:

- `POST /api/ai-evidence` with `{"id":"block-id","refresh":false}` snapshots
  the selected current block and speaker under the existing store lock.
- Alternatively send `{"segment":{"segment_id":"s42","speaker":"Lula",
  "transcription":"...","start_time":132.5,"end_time":148.9,"language":"pt-BR"}}`.
  Times are optional seconds. UI block times preserve the recorder's Unix seconds;
  direct callers may supply their own segment-relative seconds.
- A `202` response supplies `job_id` and `PROCESSING`.
- `GET /api/ai-evidence/{job_id}` returns status, input snapshot, result and error.
  The API retains the latest 50 jobs in memory and reads older jobs from disk.
  Jobs and audit events are saved locally in `data/<session>/ai_evidence/`.
  Restarted runs that did not finish appear as interrupted in the debug viewer.
  Browser refresh clears the operator's rendered panels; saved runs remain available
  at **Inspecionar IA** (`/debug`). See [the audit dashboard](ai_debug.md) for live
  steps, exact model payloads, verdict rules, comparison and JSON exports.
- `GET /api/state` includes `ai_evidence.enabled` for the button state.

`AnalysisResult` contains segment ID/speaker, original transcription, status
COMPLETED/PARTIAL/ERROR, provider/model, analysis timestamp, errors, and claims.
Each claim preserves exact quote, normalized proposition, subjects, topic,
keywords/synonyms, source segment/times, retrieval status, evidence, nullable
verdict, explanation, team ambiguity, pending review status and errors. Evidence
includes complete row fields, spreadsheet/sheet/record identities, row/range,
row URL, documentary and primary-source URLs, original team/label/type, score,
relevance, summary, retrieval time and cache state. Row URLs and sources never
come from model-generated output.
Per-record context compatibility, source quality and the model's quoted basis are
also retained for human inspection, even when Python withholds the verdict.

Transcriptions are bounded to 16,000 characters; extraction to 12 claims;
assessment to 20 candidates and a 60,000-character cell budget, with excerpts
at most 1,800 characters each. Prompts explicitly treat cells/transcripts as
untrusted data. No model tools, arbitrary API requests or executable instructions
are available. Provider errors are sanitized; keys remain in environment/key files.

## Verification

```bash
python -m unittest discover -v
node test_operator_ui.js
python evaluate_evidence_search.py
```

The first two are offline mocked checks, including no/multiple claims,
speaker/subject differences, both candidates, indicators, all four schemas,
LU106 at row 176, N01 at row 2, PF16 at row 20, stale/error behavior,
malformed JSON, rejected contextual matches, provisional verdict gates, team
ambiguity, provider switching, and background job responsiveness.

The last command reads the real workbook and evaluates seven manually labelled
positive query/record pairs plus two negatives, including paraphrases and search
synonyms. On 2026-10-23 (503 records), hit@5 and hit@10 were both 7/7; both
negatives returned no candidates. LU106 and N01 were rank 1; PF16 was rank 1–2,
and the cross-tab debt record FB205 rank 2. These few labels do not establish
general relevance quality or factual accuracy. Re-run after workbook changes and
tune the configurable weights against a larger human-labelled set.

Implementation references: [OpenAI structured responses](https://developers.openai.com/api/docs/guides/structured-outputs),
[Ollama compatibility](https://docs.ollama.com/api/openai-compatibility), and
[Google Sheets read API](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/get).
