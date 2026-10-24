# Checkability scores

`checkability.py` gives each finalized transcript block a percentage that says
how checkable it is for a fact-checker. The percentage comes from Jev,
TypeSafe's evaluation model. Jev does not generate text: it answers a typed
question about a state with calibrated probabilities. So it says *how*
checkable a block is, but not *which* phrase is checkable.

## What counts as checkable

A specific claim about the **past or present** that can be verified as true or
false: a figure, date, event, action attributed to someone, quote, law or
decision.

Not checkable: personal attacks, insults, opinion, value judgments, rhetoric,
questions, procedural talk, promises, predictions and vague comparisons ("my
government was better", "I will create 1 million jobs"). If a promise or
comparison embeds a fact ("I created 500k jobs and I will create more"), the
embedded fact counts.

A block is scored by the **best claim inside it**, never by the share of
checkable text. One checkable sentence among attacks and promises still scores
near 100.

## The question

One question named `checkability`, of type `score`, with three levels. The order
of the criteria is the order of the levels:

| Level | Meaning |
|---|---|
| 0 | No checkable claim: only attack, insult, opinion, rhetoric, question, filler, promise, prediction or vague comparison |
| 1 | Weakly checkable: a fact about the past or present, but vague, with no figure, date, name or object to verify |
| 2 | At least one specific, clearly checkable claim, even if the rest of the block is not checkable |

The exact instructions and criteria are the constants `INSTRUCTIONS` and
`CRITERIA` in `checkability.py`. **Bump `RUBRIC_VERSION`** on any change to the
question, the criteria, the formula, the thresholds or the shape of the state.
A bump makes the next run rescore every block.

## Formula

```
percentage = sum(i * p_i) / (levels - 1) * 100
```

`p_i` is Jev's probability for level `i`. For example, `[0.10, 0.20, 0.70]`
gives `(0.20 + 1.40) / 2 * 100 = 80`. The backend applies no threshold. The UI
decides later which percentages to highlight.

## What Jev receives

```json
{
  "target_block": "Isso que ele falou é mentira, e eu vou resolver.",
  "speaker": "Lula",
  "previous_blocks": [
    {"speaker": "Flávio Bolsonaro", "text": "No meu governo o desemprego caiu para 7%."}
  ]
}
```

- `target_block`: the block's current text, with operator edits applied. It is
  never truncated.
- `speaker`: the operator's label (Lula, Flávio Bolsonaro, mediador), or `null`
  when the block is unlabelled. Soniox speaker hints are not used.
- `previous_blocks`: up to 3 earlier finalized blocks, oldest first, each cut
  to its last 400 characters. Blocks end at pauses and speaker changes, so
  "that", "he" and "this number" usually point at the last few turns. The
  instructions tell Jev to use them only to resolve references and never to
  score them.

Combined blocks use the same rules: their joined text is the target, and the
blocks before the group are the context.

## When blocks are scored

`python3 checkability.py --data-dir <session> --watch` scores each block about 5
seconds after it is finalized, to give the operator time to label it. Live
blocks, gaps, empty blocks and mediator blocks are skipped.

A block is scored again only when its Jev input changes: its text, its label,
the text or labels of its context, `JEV_MODEL` or `RUBRIC_VERSION`. Without
`--watch` the script scores the pending blocks once and exits. If Jev fails,
nothing is stored for that block, and `--watch` retries 30 seconds later.

## Combined blocks

A combined block (*Combinar ↓*) gets the **maximum** of its source blocks'
scores, never the average, so a checkable sentence is not diluted by its
neighbour (90% + 5% → 90%). This costs no Jev call and is stored with
`method: "max_of_sources"` and the winning source in `best_source_id`.

The maximum is used only when the sources' scored texts, joined, are exactly the
combined text, and every source score is current for the model and rubric.
Otherwise the combined block is sent to Jev again, with the same context rules
as any block, and stored with `method: "jev_rerun"`. This happens when:

- the combined text was edited, or restored to the transcription, after
  combining;
- a source was combined before it was scored, or edited after it was scored;
- `JEV_MODEL` or `RUBRIC_VERSION` changed since the sources were scored.

Nested combinations use the original blocks as sources. Source scores are kept,
so *Descombinar* needs no new call.

**Known limitation:** the maximum can miss a claim that only appears when the
blocks are read together. This is accepted; it is handled by the operators, not
by the code. If it turns out to matter, compare `max_of_sources` with
`jev_rerun` on real sessions before changing the rule.

### Operator guidance

During the debate, the operators (one or two people) should combine as many
blocks as make sense:

- Combine a claim with the block that supports or contests it, when the page
  allows it (see below).
- Combine consecutive blocks from the same speaker that continue the same point.
- No extra step is needed: a combined block whose sources' scores do not cover
  its text is sent to Jev again automatically.

The operator page refuses to combine blocks labelled with different speakers. A
combined block has a single speaker label and is sent to Sheets and the
platform under it. So a claim and a reply from the other candidate stay
separate, and the reply is scored with the claim as context. For example, "Isso
que ele falou é mentira, e eu vou resolver." after "No meu governo o desemprego
caiu para 7%." should land in the middle band, roughly 25–60% (level 1). It
contests a specific figure but states no new fact. Read together with the
figure as one block, it scores high.

## Stored result

`data/<session>/checkability.jsonl` has one line per score. The last line per
`block_id` wins, and earlier lines remain as history.

| Field | Meaning |
|---|---|
| `block_id` | Block id, or the `merge-…` id of a combined block |
| `source_ids` | Original blocks covered by the score (just the block itself when it is not combined) |
| `best_source_id` | For a combined block scored from its sources: the source that gave the score |
| `percentage` | Formula above, 0–100, one decimal |
| `probabilities` | Jev's raw probabilities per level, level 0 first |
| `confidence` | Jev's confidence |
| `model` | Model that answered, for example `jev-1.13.0` or `jev-mock` |
| `rubric_version` | `RUBRIC_VERSION` at scoring time |
| `method` | `jev_direct` (a single block), `max_of_sources` or `jev_rerun` (a combined block) |
| `input_hash` | Hash of the state, model and rubric version; a mismatch means the score is stale |
| `state` | The exact state sent to Jev |
| `scored_at` | When the score was stored |

Because the raw probabilities are kept, the formula or the strategy for
combined blocks can change later without calling Jev again.

## Operator page

`server.py` reads `checkability.jsonl` and shows each block's score next to the
speaker:

| Badge | Range |
|---|---|
| grey outline | below 25% |
| yellow | 25% to 60% |
| green | above 60% |

The bands are `CHECK_MID` and `CHECK_HIGH` in `static/index.html`. They live
only in the UI, not in the stored scores. A faded badge means the block's text
changed since it was scored and `checkability.py` has not rescored it yet. The
tooltip shows the method, the model and the rubric version, and flags mock
scores. Unscored blocks, including mediator blocks, have no badge.

## Google Sheets

When a block is sent to Sheets, its score goes in column **E — Checabilidade**
as a whole number from 0 to 100. The AI assessment is in F, the theme from
`themes.py` in G (see [THEMES.md](THEMES.md)), and Status and Colaborador in H
and I. The
row is a snapshot taken when **Enviar** is pressed and is never updated. E is
left empty when there is no score yet, when the text changed since it was
scored, or when the score is a mock (`jev-mock`). Several blocks sent together
get the best score among them, as combined blocks do.

Scores are not sent to the Aletheia platform.

## Configuration

| Variable | Default | |
|---|---|---|
| `TYPESAFE_API_KEY` | (required) | Bearer token for the TypeSafe API |
| `JEV_MODEL` | `jev-1.13.0` | Pin it so scores are reproducible |
| `JEV_BASE_URL` | `https://api.typesafe.ai` | Requests go to `<base>/v1/systemone` |
| `JEV_TIMEOUT` | `30` | Seconds per request |
| `JEV_BACKOFF_SECONDS` | `2,5,10` | Waits between retries |

Timeouts, network errors, 408, 429 and 5xx are retried with these waits, plus or
minus 20% jitter. Other errors (400, 401, 403 and so on) and malformed answers,
such as a missing level, fail at once.

## Mock mode

With `TYPESAFE_API_KEY=your_typesafe_api_key_here`, no request is made. The
client returns answers in the real response format, seeded by the state, so the
same text always gets the same answer. These answers are **random, not real
scores**. They are stored with `model: "jev-mock"`, and a warning is logged on
every call. When a real key is set, the mock scores are replaced on the next
run. An empty or missing key is an error, never a silent switch to mock mode.

## Checking the rubric

`python3 checkability.py --check` scores the reference examples with the
configured key and prints each percentage. Use it with a real key after
changing the rubric. The unit tests (`python3 -m unittest test_checkability`)
use a fake transport. They check the request, the formula, the context, the
retries and the storage, but they cannot check Jev's judgement.
