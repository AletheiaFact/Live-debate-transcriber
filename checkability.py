#!/usr/bin/env python3
"""Score how checkable each transcript block is, with Jev (see CHECKABILITY.md).

Runs separately from recorder.py and server.py and only reads their files,
except checkability.jsonl, which only this script writes.

    python3 checkability.py --data-dir data/debate           # score pending blocks once
    python3 checkability.py --data-dir data/debate --watch   # keep scoring as blocks finalize
    python3 checkability.py --check                          # score the reference examples

A block is (re)scored when its Jev input changes: text, speaker, context,
model or RUBRIC_VERSION. A combined block takes the best score of its sources,
or is sent to Jev again when those scores do not cover its current text.
"""

import argparse
import hashlib
import json
import os
import sys
import time

import jev_client
from jev_client import JevClient, JevError, log
from platform_client import load_dotenv, now_iso
from server import HERE, Store, to_epoch

# Bump on any change to the question, criteria, formula, thresholds or state shape.
RUBRIC_VERSION = 1
QUESTION_NAME = "checkability"
INSTRUCTIONS = (
    "You are a triage analyst at a Brazilian fact-checking agency, working on a live debate "
    "transcript. Judge ONLY the target block. Look for the single most checkable claim "
    "anywhere inside it: a word, phrase or sentence that states a specific fact about the "
    "past or present that can be checked as true or false. The score depends ONLY on the "
    "best claim found, never on how much of the block is checkable. Surrounding attacks, "
    "insults, opinion, rhetoric, promises, predictions and vague comparisons do not lower the "
    "score and do not raise it. Use earlier blocks and the speaker only to resolve references "
    "such as 'that', 'he', 'this number'; never score the context itself."
)
# Level 0 first: the order of the criteria is the order of the levels.
CRITERIA = [
    "No checkable claim anywhere in the block: only attack, insult, opinion, rhetoric, question, "
    "filler, promise, prediction or vague comparison",
    "The best claim is weakly checkable: it states a fact about the past or present, but it is "
    "vague or lacks a figure, date, name or object that could be verified (e.g. 'the economy "
    "collapsed under your government')",
    "The block contains at least one specific, clearly checkable claim about the past or present "
    "(figure, date, event, attributed action, quote, law or decision), even if the rest of the "
    "block is not checkable",
]
QUESTIONS = {QUESTION_NAME: {"type": "score", "instructions": INSTRUCTIONS, "criteria": CRITERIA}}

# Blocks end at pauses and speaker changes, so "that"/"he"/"this number" usually
# point at the previous few turns. Older context is noise and cost.
CONTEXT_BLOCKS = 3
CONTEXT_CHARS = 400  # per previous block, keeping its end; the target is never cut
SPEAKERS = {"1": "Lula", "2": "Flávio Bolsonaro", "M": "mediador"}
SETTLE_SECONDS = 5  # give the operator time to label a just-finalized block
METHOD_JEV_DIRECT = "jev_direct"
METHOD_MAX_OF_SOURCES = "max_of_sources"
METHOD_JEV_RERUN = "jev_rerun"


def percentage(probabilities):
    """sum(i * p_i) / (levels - 1) * 100."""
    levels = len(probabilities)
    return round(sum(i * p for i, p in enumerate(probabilities)) / (levels - 1) * 100, 1)


def _tail(text, limit):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return "…" + text[-limit:].split(" ", 1)[-1]


def is_usable(block):
    """Finalized, transcribed and non-empty: gaps and live blocks are never scored or used as context."""
    return block.get("status") == "ok" and block.get("final") is not False and bool(block.get("text", "").strip())


def build_state(block, previous, labels):
    """Jev state: the target block, apart from the speaker and up to CONTEXT_BLOCKS earlier blocks.

    block: a current block, single or combined (its text already includes edits).
    previous: the usable blocks before it, oldest first.
    """
    return {
        "target_block": " ".join(block["text"].split()),
        "speaker": SPEAKERS.get(labels.get(block["id"])),
        "previous_blocks": [
            {"speaker": SPEAKERS.get(labels.get(b["id"])), "text": _tail(b["text"], CONTEXT_CHARS)}
            for b in previous[-CONTEXT_BLOCKS:]
        ],
    }


def score_state(client, state, method=METHOD_JEV_DIRECT):
    """Returns {percentage, probabilities, confidence, model, rubric_version, method}."""
    result = client.evaluate(state, QUESTIONS)
    answer = result["answers"][QUESTION_NAME]
    return {
        "percentage": percentage(answer["probabilities"]),
        "probabilities": answer["probabilities"],
        "confidence": answer["confidence"],
        "model": result["model"],
        "rubric_version": RUBRIC_VERSION,
        "method": method,
    }


def score_block(client, block, previous, labels):
    return score_state(client, build_state(block, previous, labels))


def input_hash(state, model, sources=()):
    key = {"state": state, "model": model, "rubric_version": RUBRIC_VERSION}
    if sources:
        key["sources"] = [s["input_hash"] for s in sources]
    return hashlib.sha1(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def covering_sources(block, scores, model):
    """Current source scores of a combined block, or None when max_of_sources would be wrong.

    The maximum only describes the combined block when its sources' scored texts,
    joined, are exactly its text: an edit or restore after combining, a source
    never scored, or a score from another model or rubric means calling Jev again.
    """
    sources = [scores.get(sid) for sid in block["source_ids"]]
    if any(s is None or s.get("method") != METHOD_JEV_DIRECT or s.get("rubric_version") != RUBRIC_VERSION
           or input_hash(s["state"], model) != s["input_hash"] for s in sources):
        return None
    joined = " ".join(s["state"]["target_block"] for s in sources)
    return sources if joined == " ".join(block["text"].split()) else None


def max_of_sources(sources):
    """The best source's result, never an average: one checkable sentence must not be diluted."""
    best = max(sources, key=lambda s: s["percentage"])
    result = {k: best[k] for k in ("percentage", "probabilities", "confidence", "model", "rubric_version")}
    return {**result, "method": METHOD_MAX_OF_SOURCES}, best["block_id"]


class Scores:
    """<data-dir>/checkability.jsonl, one line per score; the last line per block id wins.

    Each line keeps the raw probabilities, so the percentage formula and the
    strategy for combined blocks can change later without calling Jev again.
    """

    FILENAME = "checkability.jsonl"

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, self.FILENAME)
        self.items = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    self.items[item["block_id"]] = item

    def get(self, block_id):
        return self.items.get(block_id)

    def save(self, block, result, state, digest, best_source_id=None):
        item = {
            "block_id": block["id"],
            "source_ids": block.get("source_ids", [block["id"]]),
            "best_source_id": best_source_id,  # the source that gave the score, for max_of_sources
            **result,
            "input_hash": digest,
            "state": state,
            "scored_at": now_iso(),
        }
        return self.append(item)

    def append(self, item):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.items[item["block_id"]] = item
        return item


def pending(data_dir, scores, model, now=None, sources_for=covering_sources, digest_for=input_hash):
    """(block, state, digest, sources) for every block whose stored result is missing or stale.

    sources: the source results a combined block is derived from, or None to call Jev.
    themes.py passes its own sources_for and digest_for, so both skip and
    contextualise blocks the same way.
    """
    store = Store(data_dir)  # fresh each pass: the server rewrites labels, edits and merges
    with store.lock:
        blocks = [b for b in store.current_blocks() if is_usable(b)]
        labels = dict(store.labels)
    now = time.time() if now is None else now
    out = []
    for i, block in enumerate(blocks):
        if labels.get(block["id"]) == "M":
            continue  # the mediator asks, it does not claim
        settled = to_epoch(block.get("transcribed_at"))
        if not block.get("merged") and settled is not None and now - settled < SETTLE_SECONDS:
            continue
        state = build_state(block, blocks[:i], labels)
        sources = sources_for(block, scores, model) if block.get("merged") else None
        digest = digest_for(state, model, sources or ())
        stored = scores.get(block["id"])
        if not stored or stored.get("input_hash") != digest:
            out.append((block, state, digest, sources))
    return out


def hashed_model(client):
    """The model in input hashes: mock results must not satisfy a real key later."""
    return jev_client.MOCK_MODEL if client.is_mock else client.cfg.model


def score_pending(data_dir, client, scores):
    """Score every pending block once; returns how many were scored. JevError stops the pass."""
    done = 0
    for block, state, digest, sources in pending(data_dir, scores, hashed_model(client)):
        if sources:
            result, best = max_of_sources(sources)
            item = scores.save(block, result, state, digest, best_source_id=best)
        else:
            method = METHOD_JEV_RERUN if block.get("merged") else METHOD_JEV_DIRECT
            item = scores.save(block, score_state(client, state, method), state, digest)
        log(f"{block['id']}: {item['percentage']:5.1f}% {item['method']:<14} {state['target_block'][:70]}")
        done += 1
    return done


CHECK_CASES = [
    ("Você é um mentiroso e todo mundo sabe", []),
    ("Meu governo foi melhor que o seu", []),
    ("Vou criar 1 milhão de empregos", []),
    ("O desemprego caiu de 11% para 7% entre 2019 e 2022", []),
    ("Vou criar 1 milhão de empregos. Em 2022 o desemprego caiu de 11% para 7%.", []),
    ("Você é um mentiroso. Meu governo foi melhor e o seu foi um desastre.", []),
    ("Isso que ele falou é mentira, e eu vou resolver.",
     [("2", "No meu governo o desemprego caiu para 7%.")]),
]


def check_states(cases=CHECK_CASES):
    """The reference cases as Jev states, through the same build_state as real blocks."""
    states = []
    for text, previous, *_ in cases:
        blocks = [{"id": f"p{i}", "text": t} for i, (_, t) in enumerate(previous)]
        labels = {f"p{i}": label for i, (label, _) in enumerate(previous)}
        labels["target"] = "1"
        states.append(build_state({"id": "target", "text": text}, blocks, labels))
    return states


def parse_args(doc, check_help):
    load_dotenv()
    p = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=os.path.join(HERE, "data", "live"))
    p.add_argument("--watch", action="store_true", help="keep scoring as blocks finalize (Ctrl+C stops)")
    p.add_argument("--check", action="store_true", help=check_help)
    return p.parse_args()


def connect(rubric):
    """A JevClient for the configured key; exits when the key is empty."""
    cfg = jev_client.Config()
    if not cfg.api_key:
        sys.exit(f"TYPESAFE_API_KEY is not set; use '{jev_client.TYPESAFE_API_KEY_PLACEHOLDER}' for mock answers")
    client = JevClient(cfg)
    if client.is_mock:
        log(f"model {jev_client.MOCK_MODEL}: placeholder key, answers are random, not real results")
    else:
        log(f"model {cfg.model}, {rubric}")
    return client


def run(data_dir, watch, run_pass):
    """run_pass() once, or every 2 s with watch; a Jev failure retries the pending blocks 30 s later."""
    if not os.path.isdir(data_dir):
        sys.exit(f"no session at {data_dir}")
    try:
        while True:
            try:
                run_pass()
                wait = 2
            except JevError as e:
                if not watch:
                    sys.exit(f"Jev failed: {e}")
                log(f"Jev failed, will retry the pending blocks later: {e}")
                wait = 30
            if not watch:
                return
            time.sleep(wait)
    except KeyboardInterrupt:
        pass


def main():
    args = parse_args(__doc__, "score the reference examples and exit")
    client = connect(f"rubric v{RUBRIC_VERSION}")

    if args.check:
        for state in check_states():
            result = score_state(client, state)
            context = f"  (context: {state['previous_blocks'][-1]['text']})" if state["previous_blocks"] else ""
            print(f"{result['percentage']:5.1f}%  {state['target_block']}{context}")
        return

    scores = Scores(args.data_dir)
    run(args.data_dir, args.watch, lambda: score_pending(args.data_dir, client, scores))


if __name__ == "__main__":
    main()
