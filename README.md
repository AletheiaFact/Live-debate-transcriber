# Live debate transcriber

A small tool for following a live debate in real time. It transcribes the audio
in Brazilian Portuguese, lets an operator tag who is speaking, and sends each
tagged block to the [Aletheia](https://github.com/AletheiaFact/aletheia)
fact-checking platform. There, fact-checkers turn the blocks into speeches and
choose claims to check.

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
- **No dependencies:** Python 3 standard library only (no `pip install`), plain
  HTML/JS, plus `ffmpeg`.

## How it works

```
                ┌──────────── recorder.py ────────────┐
audio source ──►│ ffmpeg ──► 15 s WAV chunks (archive) │
(stream, file,  │    └─────► PCM ──► Soniox WebSocket   │──► data/<session>/blocks.jsonl
 Mac audio)     └─────────────────────────────────────┘          status.json
                                                                     │
                ┌──────────── server.py ──────────────┐              │ (reads)
browser ◄──────►│ operator page on 127.0.0.1:8765      │◄─────────────┘
                │ labels.json, edits.json, outbox.jsonl│──► Aletheia API (drafts)
                └─────────────────────────────────────┘
```

The two processes only share files. If the page, the browser or `server.py`
crash, recording and transcription keep going; restart `server.py` and nothing
is lost.

## Requirements

- Python 3.10 or newer.
- `ffmpeg` on `PATH` (`brew install ffmpeg` on macOS).
- A Soniox API key (https://console.soniox.com).
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
| `S` | Send the block to the platform |
| `E` (or double-click) | Edit the block's text |
| `↑` `↓` | Move between blocks |
| `Esc` | Back to the newest block |
| `/` | Search |

- **Blocks:** a block closes when Soniox detects the end of an utterance or when
  diarization switches speaker. Very long turns are split at a sentence end after
  about 40 s. The open block updates about once per second and is marked
  `● ao vivo` until it is final.
- **Speaker hints:** the `S1`/`S2` badges come from Soniox diarization. They are
  hints only and never become labels by themselves. After you label a block,
  later blocks with the same hint show a dashed suggestion (`→ Lula?`). Hints
  restart after a reconnect.
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

## Sending blocks to Aletheia

The operator only decides who is speaking. Each block labelled 1 or 2 is sent as
it is, as a **draft** on the platform's debate. Joining blocks into speeches,
removing duplicates and choosing claims all happen on the platform.

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
   - A block that is still live can be sent right away: it waits and goes out
     with its final text.
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
contexto.txt         context terms for the 2026 debate

data/<session>/blocks.jsonl   one JSON line per block update (last line per id wins)
data/<session>/labels.json    speaker labels (server.py)
data/<session>/edits.json     text edits (server.py)
data/<session>/outbox.jsonl   send queue (server.py)
data/<session>/status.json    recorder heartbeat, every 2 s
data/<session>/audio/         16 kHz mono WAV chunks
```

`data/`, `.env` and `.aletheia_session.json` are git-ignored. Never commit them.

## License

GPL-3.0, the same as the Aletheia platform. See [LICENSE](LICENSE).
