"""Soniox real-time transcription: 16 kHz mono PCM in, blocks out.

Threads:
- the PCM reader (pcm_reader) only drains ffmpeg's stdout into an in-memory
  queue, so a slow or dead WebSocket can never stall ffmpeg or the WAV archive;
- the worker (SonioxStreamer.run) owns the WebSocket: it sends queued audio,
  reads tokens, turns them into blocks and reconnects when anything breaks.

Audio sent but not yet finalized by Soniox is kept and re-sent after a
reconnect. Audio that has to be dropped (backlog too long) becomes a visible
"transcription gap" block pointing at the archived WAV chunks.

Soniox docs: https://soniox.com/docs/stt/rt/real-time-transcription
"""

import bisect
import json
import os
import random
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime

from wsclient import ConnectionClosed, HandshakeError, WebSocket

BYTES_PER_MS = 32  # 16 kHz * 16-bit * mono
DEFAULT_MODEL = "stt-rt-v5"
REGION_HOSTS = {
    "us": "stt-rt.soniox.com",
    "eu": "stt-rt.eu.soniox.com",
    "jp": "stt-rt.jp.soniox.com",
    "in": "stt-rt.in.soniox.com",
}

# Soniox wants audio at (near) real-time speed; a backlog is replayed at most
# this much faster than real time, after an initial burst.
CATCHUP_RATE = 1.5
BURST_MS = 3000
MAX_BACKLOG_MS = 120_000          # older audio is dropped and shown as a gap
ROTATE_AFTER_MS = 270 * 60_000    # sessions are capped at 300 min of audio
KEEPALIVE_IDLE_S = 5              # server ends the stream after 20 s idle
DEAD_AFTER_S = 20                 # audio flowing but no reply: assume dead connection
FINISH_WAIT_S = 20                # wait for final tokens after ending a stream
FINISH_RETRY_S = 30               # on shutdown, give up reconnecting after this
BLOCK_SOFT_MAX_S = 40             # split very long turns at the next sentence end
WRITE_INTERVAL_S = 1.0            # throttle for rewriting the open block

DEFAULT_TERMS = [
    "Lula", "Luiz Inácio Lula da Silva", "Flávio Bolsonaro", "César Tralli", "TV Globo",
]


class SonioxError(Exception):
    def __init__(self, data):
        self.code = data.get("error_code")
        self.type = data.get("error_type")
        super().__init__(f"Soniox error {self.code} {self.type or ''}: {data.get('error_message', '')}".strip())


def iso(wall):
    return datetime.fromtimestamp(wall).astimezone().isoformat(timespec="seconds")


def hms(wall):
    return datetime.fromtimestamp(wall).strftime("%H:%M:%S")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


DEFAULT_GENERAL = {
    "idioma": "Português do Brasil",
    "assunto": "Política, debate eleitoral",
    "tema": "Debate presidencial das eleições de 2026 no Brasil",
    "local": "Debate ao vivo na TV Globo, mediado por César Tralli",
    "participantes": "Luiz Inácio Lula da Silva (Lula), Flávio Bolsonaro, César Tralli (mediador)",
}
CONTEXT_MAX_CHARS = 10_000  # Soniox limit is 8000 tokens (~10k characters)


def load_context(path):
    """Context file: "key: value" lines are general info, every other line is a term.

    "texto: ..." lines are joined into Soniox's free-text context instead.
    Returns (general dict, terms list); general["texto"] holds the text.
    Lines starting with # are comments.
    """
    general, terms = {}, []
    if not path:
        return general, terms
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition(":")
            if sep and key.strip() and value.strip() and len(key) <= 40:
                key = key.strip().lower()
                if key == "texto" and key in general:
                    general[key] += " " + value.strip()
                else:
                    general[key] = value.strip()
            else:
                terms.append(line)
    return general, terms


def build_context(cfg):
    general = {**DEFAULT_GENERAL, **cfg.extra_general}
    text = general.pop("texto", "")
    terms = list(dict.fromkeys(DEFAULT_TERMS + cfg.extra_terms))
    context = {
        "general": [{"key": k, "value": v} for k, v in general.items()],
        "terms": terms,
    }
    if text:
        context["text"] = text
    return context


def build_config(cfg):
    return {
        "model": cfg.model,
        "audio_format": "pcm_s16le",
        "sample_rate": 16000,
        "num_channels": 1,
        "language_hints": ["pt"],
        "language_hints_strict": True,
        "enable_speaker_diarization": True,
        "enable_endpoint_detection": True,
        "context": build_context(cfg),
        "client_reference_id": "debate-recorder",
    }


def make_connect(cfg):
    url = f"wss://{REGION_HOSTS[cfg.region]}/transcribe-websocket"

    def connect():
        return WebSocket(url, headers={"Authorization": f"Bearer {cfg.api_key}"})

    return connect


def pcm_reader(stream, streamer):
    """Drain ffmpeg's PCM pipe into the streamer; never blocks on the network."""
    carry = b""
    fd = stream.fileno()
    while True:
        data = os.read(fd, 3200)  # ~100 ms
        if not data:
            return
        data = carry + data
        cut = len(data) - len(data) % 2  # keep 16-bit samples aligned
        carry = data[cut:]
        if cut:
            streamer.feed(data[:cut], time.time())


class Piece:
    __slots__ = ("wall", "data", "ms")

    def __init__(self, wall, data):
        self.wall = wall          # wall clock of the first sample
        self.data = data
        self.ms = len(data) / BYTES_PER_MS


class BlockBuilder:
    """Turns Soniox tokens into blocks; one open block at a time."""

    def __init__(self, blocks, on_token):
        self.blocks = blocks
        self.on_token = on_token
        self.cur = None
        self.seq = 0
        self.session = ""
        self.dirty = False
        self.last_write = 0.0

    def new_session(self, tag):
        self.close()
        self.session = tag

    def process(self, tokens, to_wall):
        nonfinal = []
        for t in tokens:
            text = t.get("text", "")
            if not t.get("is_final"):
                if text not in ("<end>", "<fin>"):
                    nonfinal.append(t)
                continue
            if text in ("<end>", "<fin>"):
                self.close()
                continue
            cur = self.cur
            if cur and cur["finals"]:
                spk = t.get("speaker")
                speaker_changed = spk and cur["speaker"] and spk != cur["speaker"]
                too_long = (cur["end"] - cur["start"] > BLOCK_SOFT_MAX_S
                            and cur["finals"][-1].rstrip().endswith((".", "?", "!")))
                if speaker_changed or too_long:
                    self.close()
            self._ensure(t, to_wall)
            self.cur["finals"].append(text)
            self.cur["speaker"] = self.cur["speaker"] or t.get("speaker")
            self._extend(t, to_wall)
        if nonfinal:
            self._ensure(nonfinal[0], to_wall)
            for t in nonfinal:
                self._extend(t, to_wall)
        if self.cur is not None:
            tail = "".join(t["text"] for t in nonfinal)
            if tail != self.cur["nonfinal"]:
                self.cur["nonfinal"] = tail
                self.dirty = True
        if tokens:
            self.on_token()

    def _ensure(self, tok, to_wall):
        if self.cur is None:
            self.seq += 1
            start = to_wall(tok.get("start_ms", 0))
            self.cur = {
                "id": f"rt-{self.session}-{self.seq:04d}",
                "start": start,
                "end": start,
                "received_at": time.time(),
                "speaker": tok.get("speaker"),
                "finals": [],
                "nonfinal": "",
                "written": False,
            }
        self.dirty = True

    def _extend(self, tok, to_wall):
        if "end_ms" in tok:
            self.cur["end"] = max(self.cur["end"], to_wall(tok["end_ms"]))

    def _write(self, final):
        cur = self.cur
        text = ("".join(cur["finals"]) + cur["nonfinal"]).strip()
        if not text and not cur["written"]:
            return
        self.blocks.write({
            "id": cur["id"],
            "start": iso(cur["start"]),
            "received_at": iso(cur["received_at"]),
            "status": "ok",
            "text": text,
            "error": None,
            "audio": None,
            "transcribed_at": iso(time.time()),
            "final": final,
            "speaker_hint": cur["speaker"],
            "start_ms": int(cur["start"] * 1000),
            "end_ms": int(cur["end"] * 1000),
        })
        cur["written"] = True
        self.dirty = False
        self.last_write = time.monotonic()

    def tick(self):
        if self.cur and self.dirty and time.monotonic() - self.last_write >= WRITE_INTERVAL_S:
            self._write(final=False)

    def drop_nonfinal(self):
        if self.cur:
            self.cur["nonfinal"] = ""

    def close(self):
        if self.cur:
            self.cur["nonfinal"] = ""
            self._write(final=True)
            self.cur = None


class GapTracker:
    """Collects dropped audio intervals and shows each as a failed block."""

    def __init__(self, blocks, chunk_paths):
        self.blocks = blocks
        self.chunk_paths = chunk_paths
        self.cur = None   # [id, start, end]
        self.count = 0
        self.dirty = False
        self.last_write = 0.0

    def add(self, start, end):
        if self.cur and start - self.cur[2] < 1.0:
            self.cur[2] = max(self.cur[2], end)
        else:
            self.flush(force=True)
            self.count += 1
            self.cur = [f"gap-{datetime.fromtimestamp(start):%d%H%M%S}-{self.count}", start, end]
            log(f"GAP started at {hms(start)}: backlog full, dropping audio (it stays in the WAV archive)")
        self.dirty = True

    def flush(self, force=False):
        if not (self.cur and self.dirty):
            return
        if not force and time.monotonic() - self.last_write < 2:
            return
        gid, start, end = self.cur
        paths = self.chunk_paths(start, end)
        self.blocks.write({
            "id": gid,
            "start": iso(start),
            "received_at": iso(time.time()),
            "status": "failed",
            "text": "",
            "error": f"lacuna na transcrição {hms(start)}–{hms(end)}, áudio salvo em {', '.join(paths) or '(nenhum arquivo de trecho encontrado)'}",
            "audio": "; ".join(paths),
            "final": True,
            "start_ms": int(start * 1000),
            "end_ms": int(end * 1000),
        })
        self.dirty = False
        self.last_write = time.monotonic()


class SonioxStreamer:
    def __init__(self, cfg, blocks, chunk_paths, connect):
        self.cfg = cfg
        self.connect = connect
        self.lock = threading.Lock()
        self.queue = deque()      # pieces not sent yet
        self.queued_ms = 0.0
        self.dropped = []         # (start, end) walls, filled by feed(), drained by the worker
        self.finishing = threading.Event()
        self.done = threading.Event()
        self.builder = BlockBuilder(blocks, self._on_token)
        self.gaps = GapTracker(blocks, chunk_paths)
        self.session_no = 0
        self.state = {
            "provider": "soniox",
            "region": cfg.region,
            "ws_state": "starting",
            "ws_error": None,
            "last_token_at": None,
            "reconnects": 0,
            "sessions": 0,
            "gaps": 0,
            "backlog_s": 0,
        }
        self.thread = threading.Thread(target=self.run, name="soniox", daemon=True)

    # --- called from other threads -------------------------------------------------

    def start(self):
        self.thread.start()

    def feed(self, data, wall_read):
        piece = Piece(wall_read - len(data) / BYTES_PER_MS / 1000, data)
        with self.lock:
            self.queue.append(piece)
            self.queued_ms += piece.ms
            self._trim_backlog()

    def status(self):
        with self.lock:
            backlog = self.queued_ms
        return {**self.state, "backlog_s": round(backlog / 1000), "gaps": self.gaps.count}

    def finish(self, timeout):
        self.finishing.set()
        self.done.wait(timeout)

    # --- worker --------------------------------------------------------------------

    def _trim_backlog(self):
        # Caller holds self.lock.
        while self.queued_ms > MAX_BACKLOG_MS and self.queue:
            old = self.queue.popleft()
            self.queued_ms -= old.ms
            self.dropped.append((old.wall, old.wall + old.ms / 1000))

    def _requeue(self, pieces):
        with self.lock:
            for p in reversed(pieces):
                self.queue.appendleft(p)
                self.queued_ms += p.ms
            self._trim_backlog()

    def _pop(self):
        with self.lock:
            if not self.queue:
                return None
            p = self.queue.popleft()
            self.queued_ms -= p.ms
            return p

    def _queue_empty(self):
        with self.lock:
            return not self.queue

    def _drain_dropped(self):
        with self.lock:
            dropped, self.dropped = self.dropped, []
        for start, end in dropped:
            self.gaps.add(start, end)
        self.gaps.flush()

    def _on_token(self):
        self.state["last_token_at"] = iso(time.time())

    def _set(self, ws_state, error=None):
        self.state["ws_state"] = ws_state
        if error is not None:
            self.state["ws_error"] = error

    def run(self):
        backoff = 1
        give_up_at = None
        try:
            while True:
                if self.finishing.is_set():
                    if self._queue_empty():
                        break
                    give_up_at = give_up_at or time.monotonic() + FINISH_RETRY_S
                    if time.monotonic() > give_up_at:
                        log("Soniox: giving up on the remaining backlog")
                        break
                connected = False
                try:
                    self._set("connecting")
                    ws = self.connect()
                    connected = True
                    backoff = 1
                    finished = self.session(ws)
                    if finished:
                        break
                except Exception as e:
                    msg = str(e)
                    log(f"Soniox: {msg}; reconnecting in {backoff}s")
                    self._set("reconnecting", msg)
                    if connected:
                        self.state["reconnects"] += 1
                    self._drain_dropped()
                    # Wait, but keep gap blocks current while disconnected.
                    deadline = time.monotonic() + backoff
                    while time.monotonic() < deadline:
                        time.sleep(0.2)
                        self._drain_dropped()
                    backoff = min(backoff * 2, 10)
        finally:
            self.builder.close()
            with self.lock:
                leftover = list(self.queue)
                self.queue.clear()
                self.queued_ms = 0
            for p in leftover:
                self.dropped.append((p.wall, p.wall + p.ms / 1000))
            self._drain_dropped()
            self.gaps.flush(force=True)
            self._set("closed")
            self.done.set()

    def session(self, ws):
        """Run one Soniox stream. Returns True when the recorder is finishing and all audio is done."""
        self.session_no += 1
        self.state["sessions"] = self.session_no
        self.builder.new_session(f"{datetime.now():%d%H%M%S}{self.session_no}")
        anchor_ms, anchor_wall = [], []
        unacked = deque()          # (session_ms_end, piece) not yet finalized by Soniox
        sent_ms = 0.0
        sent_at_last_msg = 0.0
        budget = BURST_MS
        now = time.monotonic()
        last_tick = last_send = last_msg = session_start = now
        ended = False
        end_deadline = None

        def to_wall(ms):
            i = max(0, bisect.bisect_right(anchor_ms, ms) - 1)
            if not anchor_ms:
                return time.time()
            return anchor_wall[i] + (ms - anchor_ms[i]) / 1000

        try:
            ws.send_text(json.dumps(build_config(self.cfg), ensure_ascii=False))
            self._set("connected", "")
            log(f"Soniox: session {self.session_no} started ({self.cfg.region})")
            while True:
                now = time.monotonic()
                budget = min(BURST_MS, budget + (now - last_tick) * 1000 * CATCHUP_RATE)
                last_tick = now

                while not ended and budget > 0:
                    piece = self._pop()
                    if piece is None:
                        break
                    unacked.append((sent_ms + piece.ms, piece))
                    anchor_ms.append(sent_ms)
                    anchor_wall.append(piece.wall)
                    sent_ms += piece.ms
                    budget -= piece.ms
                    ws.send_binary(piece.data)
                    last_send = now

                if not ended:
                    rotate = max(sent_ms, (now - session_start) * 1000) >= ROTATE_AFTER_MS
                    finishing = self.finishing.is_set() and self._queue_empty()
                    if rotate or finishing:
                        ws.send_text("")  # empty text frame = end of audio
                        ended, end_deadline = True, now + FINISH_WAIT_S
                        self._set("finishing" if finishing else "rotating")
                        log("Soniox: ending session " + ("(shutdown)" if finishing else "(rotation before 300 min limit)"))
                    elif now - last_send > KEEPALIVE_IDLE_S:
                        ws.send_text('{"type": "keepalive"}')
                        last_send = now

                msg = ws.recv(timeout=0.05)
                if msg is not None:
                    kind, payload = msg
                    if kind == "close":
                        raise ConnectionClosed(f"server closed the connection {payload}")
                    if kind == "text":
                        last_msg, sent_at_last_msg = now, sent_ms
                        data = json.loads(payload)
                        if data.get("error_code") is not None:
                            raise SonioxError(data)
                        self.builder.process(data.get("tokens") or [], to_wall)
                        final_ms = data.get("final_audio_proc_ms", 0)
                        while unacked and unacked[0][0] <= final_ms + 1:  # ms are rounded
                            unacked.popleft()
                        if data.get("finished"):
                            self.builder.close()
                            ws.close()
                            if unacked and not ended:  # server ended early: resend in the next session
                                self._requeue([p for _, p in unacked])
                            return ended and self.finishing.is_set()

                if sent_ms - sent_at_last_msg > DEAD_AFTER_S * 1000 and now - last_msg > DEAD_AFTER_S:
                    raise ConnectionClosed(f"no reply from Soniox for {DEAD_AFTER_S}s")
                if ended and now > end_deadline:
                    raise ConnectionClosed("timed out waiting for final tokens")
                self.builder.tick()
                self._drain_dropped()
        except BaseException:
            # Whatever Soniox did not finalize goes back to the queue for the next session.
            self.builder.drop_nonfinal()
            self.builder.close()
            self._requeue([p for _, p in unacked])
            ws.close_socket()
            raise


# --- --fake: a stand-in for the Soniox WebSocket ------------------------------------

FAKE_LINES = [
    ("1", "Em 2022 o desemprego caiu para 8,1% e criamos 2 milhões de empregos."),
    ("2", "Isso não é verdade, a inflação passou de 10% em 2021."),
    ("3", "Senhores candidatos, o tempo de resposta é de 1 minuto e 30 segundos."),
    ("1", "O orçamento da saúde subiu R$ 50 bilhões desde 1º de janeiro de 2023."),
    ("2", "Quarenta por cento das famílias estão endividadas, segundo dados de 15/08/2026."),
]


class FakeSoniox:
    """Produces Soniox-shaped token messages from the amount of audio received."""

    WORD_MS = 350

    def __init__(self, drop_after_s):
        self.drop_at = time.monotonic() + drop_after_s if drop_after_s else None
        self.audio_ms = 0.0
        self.out = deque()
        self.words = deque()     # words of the current line not emitted yet
        self.tail = []           # emitted, not final yet
        self.final_ms = 0
        self.speaker = "1"
        self.next_at = 800
        self.ended = False

    def _check_drop(self):
        if self.drop_at and time.monotonic() > self.drop_at:
            raise ConnectionClosed("simulated network drop (--fake-drop-every)")

    def send_text(self, text):
        self._check_drop()
        if text == "":
            finals = self.tail + [{"text": "<end>", "is_final": True}]
            self._emit(finals, [], finished=True)
            self.ended = True

    def send_binary(self, data):
        self._check_drop()
        self.audio_ms += len(data) / BYTES_PER_MS
        while self.audio_ms >= self.next_at:
            if not self.words:
                if self.tail:
                    self._emit(self.tail + [{"text": "<end>", "is_final": True}], [])
                    self.tail = []
                    self.next_at += 1000
                    continue
                self.speaker, line = random.choice(FAKE_LINES)
                self.words.extend(line.split())
            word = self.words.popleft()
            start = self.next_at
            tok = {"text": " " + word, "start_ms": int(start),
                   "end_ms": int(start + self.WORD_MS), "speaker": self.speaker, "is_final": False}
            self.tail.append(tok)
            self.next_at += self.WORD_MS
            finals, self.tail = self.tail[:-3], self.tail[-3:]
            self._emit(finals, self.tail)

    def _emit(self, finals, nonfinal, finished=False):
        finals = [{**t, "is_final": True} for t in finals]
        for t in finals:
            self.final_ms = max(self.final_ms, t.get("end_ms", 0))
        msg = {"tokens": finals + nonfinal, "final_audio_proc_ms": self.final_ms,
               "total_audio_proc_ms": int(self.audio_ms)}
        if finished:
            msg = {"tokens": finals, "final_audio_proc_ms": int(self.audio_ms),
                   "total_audio_proc_ms": int(self.audio_ms)}
            self.out.append(json.dumps(msg))
            msg = {"tokens": [], "final_audio_proc_ms": int(self.audio_ms),
                   "total_audio_proc_ms": int(self.audio_ms), "finished": True}
        self.out.append(json.dumps(msg))

    def recv(self, timeout):
        self._check_drop()
        if self.out:
            return "text", self.out.popleft()
        time.sleep(min(timeout, 0.02))
        return None

    def close(self):
        pass

    def close_socket(self):
        pass


def make_fake_connect(drop_every_s, outage_s=8):
    state = {"down_until": 0.0}

    def connect():
        if time.monotonic() < state["down_until"]:
            raise ConnectionClosed("simulated outage: connect refused")
        conn = FakeSoniox(drop_every_s)
        if conn.drop_at:
            state["down_until"] = conn.drop_at + outage_s
        return conn

    return connect


def describe_context(cfg):
    ctx = build_context(cfg)
    print(f"Context ({cfg.context_source}):")
    for item in ctx["general"]:
        print(f"  {item['key']}: {item['value']}")
    if ctx.get("text"):
        print(f"  texto: {ctx['text']}")
    print(f"  {len(ctx['terms'])} terms: {', '.join(ctx['terms'])}")
    print(f"  size: {len(json.dumps(ctx, ensure_ascii=False))} of ~{CONTEXT_MAX_CHARS} characters")


# --- --check ---------------------------------------------------------------------------

CHECK_TEXT = "Boa noite. Este é um teste de transcrição do debate entre Lula e Flávio Bolsonaro na TV Globo."


def check(cfg):
    """Key, network/SSL and a short real round-trip with synthetic Portuguese speech."""
    with tempfile.TemporaryDirectory() as tmp:
        aiff = os.path.join(tmp, "check.aiff")
        try:
            subprocess.run(["say", "-v", "Luciana", CHECK_TEXT, "-o", aiff], check=True)
            pcm = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", aiff,
                 "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                check=True, capture_output=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"FAIL: could not make test audio with 'say -v Luciana' + ffmpeg: {e}")
            return 1

    describe_context(cfg)
    print(f"Connecting to {REGION_HOSTS[cfg.region]} (region {cfg.region}), model {cfg.model} ...")
    try:
        ws = make_connect(cfg)()
    except HandshakeError as e:
        print(f"FAIL: {e}")
        if e.status in (401, 403):
            print("The key was rejected. Check SONIOX_API_KEY and that SONIOX_REGION matches the key's project region.")
        return 1
    except Exception as e:
        print(f"FAIL: cannot connect: {e}")
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            print("Fix: run 'Install Certificates.command' for this Python (see README).")
        return 1

    finals, speakers = [], set()
    try:
        ws.send_text(json.dumps(build_config(cfg), ensure_ascii=False))
        step = 100 * BYTES_PER_MS
        t0 = time.monotonic()
        for i in range(0, len(pcm), step):
            ws.send_binary(pcm[i:i + step])
            # pace at real time, reading replies as they come
            while time.monotonic() - t0 < (i + step) / BYTES_PER_MS / 1000:
                msg = ws.recv(timeout=0.02)
                if msg:
                    _check_collect(msg, finals, speakers)
        ws.send_text("")
        deadline = time.monotonic() + FINISH_WAIT_S
        while time.monotonic() < deadline:
            msg = ws.recv(timeout=0.5)
            if msg and _check_collect(msg, finals, speakers):
                break
        ws.close()
    except SonioxError as e:
        print(f"FAIL: {e}")
        if e.code in (401, 403):
            print("The key was rejected. Check SONIOX_API_KEY and that SONIOX_REGION matches the key's project region.")
        return 1
    except Exception as e:
        print(f"FAIL: during round-trip: {e}")
        return 1

    text = "".join(t for t in finals if t not in ("<end>", "<fin>")).strip()
    if not text:
        print("FAIL: connected, but no final text came back")
        return 1
    print(f"Sent:     {CHECK_TEXT}")
    print(f"Received: {text}")
    print(f"Speakers: {sorted(speakers) or 'none'}")
    print("OK: Soniox key, network/SSL and real-time round-trip work")
    return 0


def _check_collect(msg, finals, speakers):
    kind, payload = msg
    if kind == "close":
        raise ConnectionClosed(f"server closed the connection {payload}")
    data = json.loads(payload)
    if data.get("error_code") is not None:
        raise SonioxError(data)
    for t in data.get("tokens") or []:
        if t.get("is_final"):
            finals.append(t["text"])
            if t.get("speaker"):
                speakers.add(t["speaker"])
    return bool(data.get("finished"))
