#!/usr/bin/env python3
"""Suggest one theme per transcript block, with Jev (see THEMES.md).

Runs separately from recorder.py, server.py and checkability.py and only reads
their files, except themes.jsonl, which only this script writes.

    python3 themes.py --data-dir data/debate           # suggest themes for pending blocks once
    python3 themes.py --data-dir data/debate --watch   # keep suggesting as blocks finalize
    python3 themes.py --check                          # run the reference examples

Blocks, context, skip rules and staleness are the same as in checkability.py.
A combined block inherits its sources' theme when they all agree and cover its
text, or is sent to Jev again otherwise. Assigning blocks to reviewer teams is
out of scope: this only suggests the theme.
"""

import hashlib
import json

import checkability
from checkability import Scores, check_states, connect, hashed_model, parse_args, run
from jev_client import log
from platform_client import now_iso

# Bump on any change to the options, descriptions, instructions or state shape.
THEME_RUBRIC_VERSION = 1
QUESTION_NAME = "theme"
INSTRUCTIONS = (
    "You are a triage analyst at a Brazilian fact-checking agency, working on a live debate "
    "transcript. Choose the theme of the target block. If the block contains a checkable "
    "claim (a specific fact about the past or present), choose the theme of the most "
    "checkable claim, even if other parts of the block are about something else. If there is "
    "no checkable claim, choose the theme of the dominant subject. Choose 'Sem tema' only if "
    "the block has no subject matter at all. Use earlier blocks and the speaker only to "
    "resolve references such as 'that', 'he', 'this number'; never classify the context."
)
NO_THEME = "Sem tema"
# Option name -> description; the name is the choice key sent to Jev, in this order.
OPTIONS = {
    "Economia e finanças públicas": "Inflação, emprego, renda, tributação e contas públicas",
    "Saúde, educação e políticas sociais": "Saúde, educação, políticas sociais, fome e desigualdade",
    "Democracia, Justiça e segurança":
        "Democracia, religião, eleições, Justiça, segurança pública e legislação",
    "Meio ambiente, infraestrutura e internacional":
        "Meio ambiente, infraestrutura, relações internacionais e dados históricos",
    NO_THEME: "Procedural talk, personal attacks, insults, filler or rhetoric with no subject matter",
}
# Fixed in code, never asked to Jev.
FAMILIES = {
    "Economia e finanças públicas": "Economia e políticas sociais",
    "Saúde, educação e políticas sociais": "Economia e políticas sociais",
    "Democracia, Justiça e segurança": "Instituições e outras temáticas",
    "Meio ambiente, infraestrutura e internacional": "Instituições e outras temáticas",
    NO_THEME: None,
}
QUESTIONS = {QUESTION_NAME: {"type": "choice", "instructions": INSTRUCTIONS, "criteria": OPTIONS}}

METHOD_JEV_DIRECT = checkability.METHOD_JEV_DIRECT
METHOD_INHERITED = "inherited_from_sources"
METHOD_JEV_RERUN = checkability.METHOD_JEV_RERUN


def suggest(client, state, method=METHOD_JEV_DIRECT):
    """Returns {theme, family, probabilities, confidence, model, theme_rubric_version, method}."""
    result = client.evaluate(state, QUESTIONS)
    answer = result["answers"][QUESTION_NAME]
    return {
        "theme": answer["choice"],
        "family": FAMILIES[answer["choice"]],
        "probabilities": answer["probabilities"],
        "confidence": answer["confidence"],
        "model": result["model"],
        "theme_rubric_version": THEME_RUBRIC_VERSION,
        "method": method,
    }


def input_hash(state, model, sources=()):
    key = {"state": state, "model": model, "theme_rubric_version": THEME_RUBRIC_VERSION}
    if sources:
        key["sources"] = [s["input_hash"] for s in sources]
    return hashlib.sha1(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def agreeing_sources(block, themes, model):
    """Current source themes of a combined block when they agree and cover its text, else None.

    Different themes, a source without a current theme, an edit or restore after
    combining, or a theme from another model or rubric mean calling Jev again.
    """
    sources = [themes.get(sid) for sid in block["source_ids"]]
    if any(s is None or s.get("method") != METHOD_JEV_DIRECT
           or s.get("theme_rubric_version") != THEME_RUBRIC_VERSION
           or input_hash(s["state"], model) != s["input_hash"] for s in sources):
        return None
    if len({s["theme"] for s in sources}) != 1:
        return None
    joined = " ".join(s["state"]["target_block"] for s in sources)
    return sources if joined == " ".join(block["text"].split()) else None


def inherit(sources):
    """The shared theme, with the mean probabilities and the lowest confidence of the sources.

    Every source ranks the shared theme first, so it stays first in the mean.
    """
    probabilities = {option: sum(s["probabilities"][option] for s in sources) / len(sources) for option in OPTIONS}
    confidences = [s["confidence"] for s in sources if s["confidence"] is not None]
    first = sources[0]
    return {
        "theme": first["theme"],
        "family": first["family"],
        "probabilities": probabilities,
        "confidence": min(confidences) if confidences else None,
        "model": first["model"],
        "theme_rubric_version": THEME_RUBRIC_VERSION,
        "method": METHOD_INHERITED,
    }


class Themes(Scores):
    """<data-dir>/themes.jsonl, one line per suggestion; the last line per block id wins."""

    FILENAME = "themes.jsonl"

    def save(self, block, result, state, digest):
        return self.append({
            "block_id": block["id"],
            "source_ids": block.get("source_ids", [block["id"]]),
            **result,
            "input_hash": digest,
            "state": state,
            "scored_at": now_iso(),
        })


def theme_pending(data_dir, client, themes):
    """Suggest a theme for every pending block once; returns how many were stored. JevError stops the pass."""
    done = 0
    pending = checkability.pending(data_dir, themes, hashed_model(client),
                                   sources_for=agreeing_sources, digest_for=input_hash)
    for block, state, digest, sources in pending:
        if sources:
            result = inherit(sources)
        else:
            result = suggest(client, state, METHOD_JEV_RERUN if block.get("merged") else METHOD_JEV_DIRECT)
        item = themes.save(block, result, state, digest)
        log(f"{block['id']}: {item['theme']:<45} {item['method']:<22} {state['target_block'][:60]}")
        done += 1
    return done


# (target block, [(speaker label, previous block), ...], expected theme)
CHECK_CASES = [
    ("O desemprego caiu de 11% para 7% entre 2019 e 2022", [], "Economia e finanças públicas"),
    ("Faltam médicos nos postos de saúde e a fome voltou", [], "Saúde, educação e políticas sociais"),
    ("O Supremo decidiu que a lei eleitoral não vale", [], "Democracia, Justiça e segurança"),
    ("O desmatamento na Amazônia dobrou em 2021", [], "Meio ambiente, infraestrutura e internacional"),
    ("A plateia, por favor, não se manifeste.", [], NO_THEME),
    # Mostly about democracy and religion, but the only checkable claim is about jobs.
    ("Eu defendo a democracia, a liberdade religiosa e eleições limpas, e ele nunca defendeu nada "
     "disso, nunca. Já no meu governo o desemprego caiu de 11% para 7%.", [], "Economia e finanças públicas"),
    # Only the previous block says what was not done.
    ("Não fiz.", [("2", "Você cortou 30% do orçamento da saúde em 2019.")], "Saúde, educação e políticas sociais"),
]


def main():
    args = parse_args(__doc__, "suggest themes for the reference examples and exit")
    client = connect(f"theme rubric v{THEME_RUBRIC_VERSION}")

    if args.check:
        misses = 0
        for (_, _, expected), state in zip(CHECK_CASES, check_states(CHECK_CASES)):
            result = suggest(client, state)
            ok = result["theme"] == expected
            misses += not ok
            context = f"  (context: {state['previous_blocks'][-1]['text']})" if state["previous_blocks"] else ""
            print(f"{'ok  ' if ok else 'MISS'} {result['theme']:<45} "
                  f"p={result['probabilities'][result['theme']]:.2f}  {state['target_block']}{context}")
            if not ok:
                print(f"     expected {expected}")
        print(f"{len(CHECK_CASES) - misses}/{len(CHECK_CASES)} as expected")
        return

    themes = Themes(args.data_dir)
    run(args.data_dir, args.watch, lambda: theme_pending(args.data_dir, client, themes))


if __name__ == "__main__":
    main()
