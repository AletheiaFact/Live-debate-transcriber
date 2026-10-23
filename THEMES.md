# Theme suggestions

`themes.py` asks Jev to suggest **one theme** for each finalized transcript
block, from a closed list. It only suggests the theme. Assigning blocks to
reviewer teams is out of scope.

It shares everything except the question with `checkability.py`
([CHECKABILITY.md](CHECKABILITY.md)): the state sent to Jev, the skip rules,
the ~5 second delay after finalization, staleness through `input_hash`, retries
and mock mode.

## The question

One question named `theme`, of type `choice`. The option name is the choice key
sent to Jev. The options, in order:

| Option | Description sent to Jev | Family (fixed in code) |
|---|---|---|
| Economia e finanças públicas | Inflação, emprego, renda, tributação e contas públicas | Economia e políticas sociais |
| Saúde, educação e políticas sociais | Saúde, educação, políticas sociais, fome e desigualdade | Economia e políticas sociais |
| Democracia, Justiça e segurança | Democracia, religião, eleições, Justiça, segurança pública e legislação | Instituições e outras temáticas |
| Meio ambiente, infraestrutura e internacional | Meio ambiente, infraestrutura, relações internacionais e dados históricos | Instituições e outras temáticas |
| Sem tema | Procedural talk, personal attacks, insults, filler or rhetoric with no subject matter | none |

The instructions tell Jev to pick the theme of the most checkable claim when
there is one, otherwise the dominant subject, and `Sem tema` only when the
block has no subject matter. Earlier blocks and the speaker are used only to
resolve references. The exact text is `INSTRUCTIONS`, `OPTIONS` and `FAMILIES`
in `themes.py`. **Bump `THEME_RUBRIC_VERSION`** on any change to the options,
descriptions, instructions or state shape. It is separate from the
checkability `RUBRIC_VERSION`.

The backend stores the probabilities for every option and applies no
"uncertain" threshold. The UI decides, for example when the top two
probabilities are close.

## When blocks get a theme

```bash
python3 themes.py --data-dir data/debate --watch
python3 themes.py --check      # the reference examples, with the expected theme
```

Same rules as checkability: live blocks, gaps, empty blocks and mediator blocks
are skipped, and a block is sent again only when its text, label, context,
`JEV_MODEL` or `THEME_RUBRIC_VERSION` changes. Run it next to `checkability.py`;
each process writes its own file.

## Combined blocks

There is no maximum for themes:

- If every source block has a current theme and it is the **same** theme, and
  the sources' texts joined are exactly the combined text, the combined block
  inherits it with no Jev call (`method: "inherited_from_sources"`). Its
  probabilities are the mean of the sources' (the shared theme stays first) and
  its confidence is the lowest.
- Otherwise (different themes, a source without a current theme, the combined
  text edited or restored, another model or rubric version) the combined block
  is sent to Jev again with the same context rules (`method: "jev_rerun"`).

Source themes are kept, so *Descombinar* needs no new call.

## Stored result

`data/<session>/themes.jsonl`, one line per suggestion; the last line per
`block_id` wins.

| Field | Meaning |
|---|---|
| `block_id` | Block id, or the `merge-…` id of a combined block |
| `source_ids` | Original blocks covered (just the block itself when it is not combined) |
| `theme` | Jev's choice, one of the option names |
| `family` | From `FAMILIES`; `null` for `Sem tema` |
| `probabilities` | Jev's probability for every option, in option order |
| `confidence` | Jev's confidence |
| `model` | Model that answered, for example `jev-1.13.0` or `jev-mock` |
| `theme_rubric_version` | `THEME_RUBRIC_VERSION` at the time |
| `method` | `jev_direct`, `inherited_from_sources` or `jev_rerun` |
| `input_hash` | Hash of the state, model and rubric version; a mismatch means the theme is stale |
| `state` | The exact state sent to Jev; the UI compares its `target_block` to spot stale themes |
| `scored_at` | When the theme was stored |

## Mock mode and checks

With the placeholder key, themes are random but deterministic per state and
stored with `model: "jev-mock"`; they are replaced once a real key is set. An
empty key is an error. `--check` with a mock key therefore shows misses; use a
real key after changing the rubric. The unit tests
(`python3 -m unittest test_themes`) use a fake transport.
