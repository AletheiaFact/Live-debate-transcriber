# Live debate transcriber

A small tool for following a live debate in real time. It transcribes the audio
in Brazilian Portuguese, lets an operator tag who is speaking, and sends each
tagged block to Google Sheets or the [Aletheia](https://github.com/AletheiaFact/aletheia)
fact-checking platform. Reviewers choose claims to check at the destination.

It was built for the 2026 Brazilian presidential debate (Lula × Flávio Bolsonaro),
but only the context file and two speaker names are specific to that debate.

- **Transcription:** [Soniox](https://soniox.com) real-time speech-to-text over
  WebSocket, with speaker diarization, endpoint detection and a custom context
  (names, acronyms, topics).
- **Safety net:** every second of audio is archived as WAV chunks. A slow or dead
  network never stops capture, and any audio that could not be transcribed shows
  up as a visible gap that points to its WAV files.
- **Operator page:** a local web page (interface in Brazilian Portuguese) to tag
  speakers with the keyboard, fix text that landed in the wrong block, and send
  blocks to the platform.
- **Dependencies:** recording and Aletheia sending use Python's standard library,
  plain HTML/JS, plus `ffmpeg`. Google Sheets sending uses the Google Python clients.

## How it works

```
                ┌──────────── recorder.py ────────────┐
audio source ──►│ ffmpeg ──► 15 s WAV chunks (archive) │
(stream, file,  │    └─────► PCM ──► Soniox WebSocket   │──► data/<session>/blocks.jsonl
 Mac audio)     └─────────────────────────────────────┘          status.json
                                                                     │
                ┌──────────── server.py ──────────────┐              │ (reads)
browser ◄──────►│ operator page on 127.0.0.1:8765      │◄─────────────┘
                │ labels.json, edits.json, send queue│──► Google Sheets / Aletheia
                └─────────────────────────────────────┘
```

The two processes only share files. If the page, the browser or `server.py`
crash, recording and transcription keep going; restart `server.py` and nothing
is lost.

## Requirements

- Python 3.10 or newer.
- `ffmpeg` on `PATH` (`brew install ffmpeg` on macOS).
- A Soniox API key (https://console.soniox.com).
- For Google Sheets sending: `python -m pip install -r requirements.txt`, a
  service account JSON key, Sheets API enabled, and the spreadsheet shared with
  the service account as an Editor.
- Optional, to capture the computer's own audio on macOS:
  [BlackHole](https://github.com/ExistentialAudio/BlackHole) (`brew install blackhole-2ch`).
- Optional, to send blocks to Aletheia: a platform that includes the debate
  transcript API (AletheiaFact/aletheia PR #2655) and a fact-checker account.

`--device` capture uses macOS AVFoundation. `--url` and `--file` work anywhere
ffmpeg runs.

## Setup

```bash
cp .env.example .env        # then fill in SONIOX_API_KEY (and SONIOX_REGION if not us)
python3 recorder.py --check
```

`--check` prints the context that will be sent, then streams a few seconds of
synthetic Portuguese speech (`say -v Luciana`, macOS) to Soniox and prints what
came back. It ends with `OK` when the key, the network and TLS all work.

If it reports `CERTIFICATE_VERIFY_FAILED`, a python.org install of Python has no
root certificates yet. Run `open "/Applications/Python 3.X/Install Certificates.command"`
(adjust the version).

### Context

The recorder sends a context to Soniox so that names, acronyms and programs are
spelled correctly. The context is made of built-in defaults (Brazilian
Portuguese, the debate, the candidates and the moderator) plus `contexto.txt`,
which is loaded automatically. Use `--context-file other.txt` to load a
different file. The format is:

```
# comment
participantes: ...    # "key: value" lines are general information; a key with
                      # the same name as a default replaces it
texto: ...            # joined into one free-text background paragraph
Bolsa Família         # every other line is a term
```

Keep numbers and checkable data out of the context: it biases recognition, and
the model could "hear" the number in the context instead of the one actually
said. The limit is about 10,000 characters; the recorder refuses to start above
it.

## Running

Use the same `--data-dir` in both terminals (the default is `data/live`). Use a
new folder to start a clean session.

**Terminal 1, operator page:**

```bash
python3 server.py --data-dir data/debate
```

Open http://127.0.0.1:8765. The server only listens on localhost.

**Terminal 2, recorder.** Choose one source:

```bash
# A stream URL ffmpeg can read (HLS .m3u8, direct http, rtmp)
python3 recorder.py --data-dir data/debate --url "https://…/playlist.m3u8"

# YouTube: get the direct audio URL first (needs yt-dlp)
python3 recorder.py --data-dir data/debate --url "$(yt-dlp -g -f bestaudio 'https://www.youtube.com/watch?v=…')"

# Whatever is playing on the Mac, through BlackHole
python3 recorder.py --data-dir data/debate --device ":BlackHole 2ch"

# A recorded file, played back in real time (rehearsal)
python3 recorder.py --data-dir data/debate --file debate.mp4
```

To capture the Mac's audio and still hear it, open **Audio MIDI Setup**, create a
**Multi-Output Device** with your speakers and **BlackHole 2ch**, and select it
as the sound output. To list capture devices, run
`ffmpeg -f avfoundation -list_devices true -i ""`. The terminal app needs
microphone permission the first time.

Stop with Ctrl+C. The recorder closes the current chunk and waits for the last
tokens. If ffmpeg exits (for example, a stream hiccup), it is restarted every
2 s.

### Rehearsal without a Soniox key

```bash
python3 recorder.py --data-dir data/rehearsal --file any_audio.mp3 --fake --fake-drop-every 30
```

`--fake` emits sample sentences instead of calling Soniox. `--fake-drop-every 30`
simulates a connection drop every 30 s, followed by an 8 s outage, so you can
watch the reconnect and buffer behaviour. To make a short Portuguese test file
on macOS, run
`say -v Luciana "Em 2023 o desemprego caiu. Isso não é verdade." -o test.aiff`.

## Operator page

The page is in Brazilian Portuguese. Keys act on the outlined block: the newest
one by default, or the block you clicked or moved to with ↑/↓.

| Key | Action |
|---|---|
| `1` / `2` / `M` | Label as Lula / Flávio / mediator (mediator blocks are dimmed) |
| `0` | Clear the label |
| `S` | Send the block to the configured destination |
| `E` (or double-click) | Edit the block's text |
| `↑` `↓` | Move between blocks |
| `Esc` | Back to the newest block |
| `/` | Search |

- **Blocks:** a block closes when Soniox detects the end of an utterance or when
  diarization switches speaker. Very long turns are split at a sentence end after
  about 40 s. The open block updates about once per second and is marked
  `● ao vivo` until it is final.
- **Minutage:** enter *Início do debate* in the header and confirm its date
  (initially the recording's date). Toggle *Mostrar minutagem* to display elapsed
  `HH:MM:SS` instead of clock time. Copying uses the selected format. The start
  uses the recording's timezone; full dates handle midnight correctly, and
  blocks before the start show negative elapsed times. Settings are saved per
  session in `operator.json`; original block timestamps are preserved.
- **Live blocks:** speaker labels can be changed while transcription is ongoing.
  Editing, merging and sending require finalized blocks.
- **Merging:** *Combinar ↓* joins a finalized block with its next adjacent
  finalized block. Conflicting speaker labels prevent the merge; if only one
  block is labelled, the combined block inherits that label. Combined text can
  be edited. *Descombinar* restores the preceding blocks with their earlier
  corrections and labels; it asks before discarding later combined-text edits.
  Repeated merges can be undone one step at a time. *Restaurar original* in the
  editor keeps the grouping and restores the joined AI transcriptions. Original
  blocks remain in `blocks.jsonl`, with group structure in `operator.json`.
- **Speaker hints:** the `S1`/`S2` badges come from Soniox diarization. They are
  hints only and never become labels by themselves. After you label a block,
  later blocks with the same hint show a dashed suggestion (`→ Lula?`). Hints
  restart after a reconnect.
- **Checkability:** when `checkability.py` is running, each scored block shows
  its score next to the speaker: grey below 25%, yellow from 25% to 60%, green
  above 60%. A faded score means the text changed and a new score is pending.
  Hover for the method and model. Sending to Sheets writes the score in
  column E. Sending to the platform includes it only with
  `ALETHEIA_SEND_CHECKABILITY=1` (see CHECKABILITY.md).
- **Number highlights:** numbers, percentages, dates and amounts are highlighted,
  because those are the claims most worth checking.
- **Editing:** when the start of one person's speech lands in the previous
  person's block, press `E`, put the cursor where the other speaker starts, and
  click *Mover do cursor em diante → próximo bloco* (or *← Mover até o cursor →
  bloco anterior*). `Enter` saves, `Esc` cancels, and *Restaurar original* undoes
  the edit. Edits live in `edits.json`; the transcript (`blocks.jsonl`) is never
  changed. Only final blocks can be edited.
- **Copying:** *Copiar* copies one block as `[HH:MM:SS] Lula: text`. *Copiar Lula
  + Flávio* copies every block labelled 1 or 2 in that format. Mediator and
  unlabelled blocks are left out.
- **Status bar:** it turns red when the recorder is not running, ffmpeg is not
  capturing, Soniox is disconnected, or no new text has arrived for 60 s.
  *Lacunas* counts transcription gaps.

## Sending blocks to Google Sheets

Install the optional dependencies and configure `.env` (the recorder still needs
`SONIOX_API_KEY`):

```bash
python -m pip install -r requirements.txt
```

```dotenv
SEND_TARGET=sheets
GOOGLE_APPLICATION_CREDENTIALS=C:/Users/silas/Downloads/aletheiatranscription2026-ea33cb5faf12.json
SPREADSHEET_ID=1x7Mz2_kB-k2-2UAswGevZd3plff2L2cDDu1JGrYtPhs
GOOGLE_SHEETS_TAB=Sheet1
```

Keep the JSON key outside the repository. On another machine, use that machine's
key path. Enable the Google Sheets API and share the spreadsheet with the service
account's `client_email` as an Editor. No Google login is needed in the browser.
Run this read-only check before starting `server.py`:

```bash
python sheets_client.py
```

The tab must have the following headers in **A6:G6**. Each selected block becomes
one row, starting at **row 7** when the template has no transcript data:

| Column | Value sent |
|---|---|
| A — Início | Elapsed start, `HH:MM:SS` |
| B — Fim | Elapsed end, `HH:MM:SS` |
| C — Etiqueta | Operator's speaker label: Lula or Flávio |
| D — Transcrição | Final text, including operator corrections and merges |
| E — Checabilidade | Jev checkability score, `0`–`100` (a number), or empty when no current score is ready |
| F — Status | `Novo` |
| G — Colaborador | Left for reviewers |

To add the Checabilidade column to an existing tab, right-click column E,
choose **Insert 1 column left**, and type `Checabilidade` in E6. Existing rows,
statuses and dropdowns move to F and G. `python sheets_client.py` confirms the
headers. Rows queued before the update are sent with an empty E.

The score is the one shown on the block when you press **Enviar**. It is left
empty if `checkability.py` has not scored the block yet, if the text changed
since it was scored, or if the score is a mock. Sent rows are never updated, so
wait for the badge when the score matters.

Set **Início do debate** and its date in the operator page before sending. Sheet
timestamps always use elapsed debate time, even if the page displays clock time.
Merged blocks use the first source's start and last source's end. Time values are
literal strings; transcript text beginning with `=` is also written as text.

Label a finalized block, correct it, then click **Enviar** or press `S`. Sending
immediately disables the button and locks text, labels, merge/unmerge, and moving
text into or out of that block. A successful send shows **enviado à planilha**.
The lock survives refreshes and server restarts. Make later corrections directly
in Sheets: the app never updates or resends a successfully delivered snapshot.
Copying and navigation remain available.

The persistent queue is `data/<session>/sheets_outbox.jsonl`. A confirmed failure
shows its error on the badge, unlocks the block and enables **Tentar novamente**.
A timeout can mean Google received the row even though its reply was lost; the
app shows **verificando envio…**, keeps the snapshot locked and reconciles in the
background. The append and a unique delivery receipt are written in one atomic
Sheets batch, so retries cannot add the same queued item twice. Receipts use
developer metadata, without adding visible columns. Keep this queue with the
session; deleting it removes local delivery history. Do not remove the receipt
metadata while any send is pending. Google limits metadata storage; if that
limit is reached, a send fails without inserting its row.

New rows are appended after the tab's last row containing values; blank dropdowns
and formatting alone do not count as data. Keep this tab for transcript records,
without prefilled statuses or unrelated values below them. Gaps among existing
records are left in place. Existing rows, formatting and dropdown rules are not
rewritten. Separate operator sessions can append concurrently, but sending the
same speech from different sessions creates separate rows. Run one server per
local session directory.

If `SEND_TARGET` is omitted, a configured `SPREADSHEET_ID` selects Sheets;
otherwise the server uses the existing Aletheia integration.

To send every block to both destinations with the same **Enviar** button, set
`SEND_TARGET=sheets,platform` and configure both. Sheets stays the primary: it
locks sent blocks and drives each block's status, while the platform's progress
appears as a second item in the status bar. Sending is refused until both are
configured. If Sheets rejects a block, nothing is queued for the platform either.

## Sending blocks to Aletheia

The existing Aletheia integration sends each selected block (including a merged
block) as a **draft** on the platform's debate. The operator chooses the speaker,
corrects the text and can combine adjacent blocks before sending. Removing
duplicates and choosing claims happen on the platform. Set `SEND_TARGET=platform`
to use this integration alone, or `SEND_TARGET=sheets,platform` to send to both.

1. In `.env`, set `ALETHEIA_URL`, `ORY_URL` (the platform's Ory project URL),
   `ALETHEIA_DEBATE_ID`, `ALETHEIA_PERSONALITY_LULA`,
   `ALETHEIA_PERSONALITY_FLAVIO`, and a different `ALETHEIA_MACHINE` on each
   machine. `ALETHEIA_DEBATE_ID` is the debate's **contentId**, not the claim id
   in the page URL. `GET /api/claim/<claimId>` returns it as `contentId`.
2. Log in with your own fact-checker account. This uses Ory's native login flow:
   the password is typed in the terminal and is never stored, and only the
   session token is saved, in `.aletheia_session.json`.

   ```bash
   python3 platform_client.py login
   python3 platform_client.py whoami    # confirms the platform accepts the session
   ```
3. Start `server.py`. In the page, label a block and press `S`. The block is
   queued without a confirmation, and the selection moves to the next block.
   - A block that is still live cannot be sent; wait until it is finalized.
   - To fix a wrong speaker, change the label and press `S` again. A corrected
     draft is sent, and the old one stays on the platform to be rejected there.
   - An edited block can be sent again with the corrected text.
   - Mediator and unlabelled blocks are never sent.
4. The queue (`outbox.jsonl`) sends in the background. It retries network errors,
   5xx, 409 and 429 with backoff. On 401/403 it waits for a new `login`, and
   queued blocks then go out without a restart.

Reviewing on the platform (until a review screen exists):

```bash
python3 platform_client.py turns --status draft
python3 platform_client.py approve <turnId> [<turnId> ...]
python3 platform_client.py reject <turnId> [<turnId> ...]
```

Each draft has an `externalId` of `<machine>:<block id>:<label>`, with a text hash
appended after an edit. Pressing `S` twice on the same block is therefore skipped
by the platform. The same block sent from two machines arrives as two drafts, so
the platform side has to deduplicate them.

## Checkability scores (Jev)

`checkability.py` asks Jev, TypeSafe's evaluation model, how checkable each
finalized block is, from 0 (attacks, opinion, promises) to 100 (a specific fact
about the past or present). It runs as a third process and writes
`checkability.jsonl` in the session folder:

```bash
python3 checkability.py --data-dir data/debate --watch
python3 checkability.py --check      # scores the reference examples
```

Set `TYPESAFE_API_KEY` and `JEV_MODEL` in `.env`. See
[CHECKABILITY.md](CHECKABILITY.md) for the rubric, the formula, the context sent
with each block and mock mode.

## Resilience

- **Reconnects:** if the WebSocket drops or Soniox stops answering for 20 s, the
  recorder reconnects with backoff (1, 2, 4, 8, 10 s). Audio is buffered in
  memory meanwhile, up to 2 minutes, including audio Soniox had received but not
  finalised. It is re-sent afterwards slightly faster than real time.
- **Gaps:** only audio beyond the buffer is dropped. It appears as a red block,
  `lacuna na transcrição HH:MM:SS–HH:MM:SS, áudio salvo em <WAV files>`.
- **Long sessions:** a Soniox stream is limited to 300 minutes, so the recorder
  starts a fresh stream after 270 minutes, without losing audio.
- **Shutdown:** Ctrl+C stops ffmpeg, sends end-of-audio and waits for the final
  tokens.

## Files

```
recorder.py          capture, WAV archive, Soniox session management
soniox_stream.py     Soniox streaming, reconnects, blocks from tokens, gaps, --check, --fake
wsclient.py          minimal WebSocket client (standard library)
server.py            operator page server (localhost only)
static/index.html    operator page
platform_client.py   Aletheia login, send queue, review commands
sheets_client.py     Google Sheets sending, delivery receipts and read-only check
requirements.txt     optional Google Sheets dependencies
contexto.txt         context terms for the 2026 debate
checkability.py      Jev checkability scores per block (rubric, context, store)
jev_client.py        TypeSafe API client for Jev (retries, mock mode)

data/<session>/blocks.jsonl   one JSON line per block update (last line per id wins)
data/<session>/labels.json    speaker labels (server.py)
data/<session>/edits.json     text edits (server.py)
data/<session>/operator.json  session time settings and reversible block merges
data/<session>/outbox.jsonl   send queue (server.py)
data/<session>/sheets_outbox.jsonl  immutable Sheets snapshots and delivery status
data/<session>/checkability.jsonl  Jev scores per block (last line per block id wins)
data/<session>/status.json    recorder heartbeat, every 2 s
data/<session>/audio/         16 kHz mono WAV chunks
```

`data/`, `.env` and `.aletheia_session.json` are git-ignored. Never commit them.

## License

GPL-3.0, the same as the Aletheia platform. See [LICENSE](LICENSE).
