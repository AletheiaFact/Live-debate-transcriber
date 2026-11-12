#!/usr/bin/env python3
"""Capture audio with ffmpeg and transcribe it live with Soniox.

ffmpeg decodes the source once and writes two outputs:
- 15 s WAV chunks archived in <data-dir>/audio/ (the safety net: every second
  of audio is kept, whatever happens to the network);
- raw 16 kHz mono PCM on stdout, streamed to Soniox in real time
  (see soniox_stream.py).

Blocks are built from Soniox tokens and appended to <data-dir>/blocks.jsonl;
an open block is rewritten with the same id until it is final, and readers
keep the last line per id.

Standard library only. Needs ffmpeg on PATH.
"""

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import soniox_stream


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_dotenv(path):
    """Minimal .env loader: KEY=value lines, existing env vars win."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            os.environ.setdefault(key, value.strip().strip("'\""))


def write_json_atomic(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


class BlockLog:
    """Append-only JSONL writer shared by the streamer threads."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, block):
        line = json.dumps(block, ensure_ascii=False) + "\n"
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


# --- capture ------------------------------------------------------------------

def ffmpeg_command(args, audio_dir, list_path):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
    if args.file:
        cmd += ["-re", "-i", args.file]
    elif args.url:
        if args.url.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        cmd += ["-i", args.url]
    elif sys.platform == "win32":
        device = args.device
        if not device.startswith("audio="):
            device = "audio=" + device
        cmd += ["-f", "dshow", "-i", device]
    elif sys.platform == "darwin":
        cmd += ["-f", "avfoundation", "-i", args.device]
    else:
        raise ValueError("--device supports Windows (DirectShow) and macOS (AVFoundation); use --url or --file on this OS")
    cmd += [
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-f", "segment", "-segment_time", str(args.chunk_seconds), "-reset_timestamps", "1",
        "-strftime", "1", "-segment_list", list_path, "-segment_list_type", "csv",
        os.path.join(audio_dir, "chunk_%Y%m%d-%H%M%S.wav"),
        # Second output from the same decode: raw PCM for the Soniox streamer.
        "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
    ]
    return cmd


def chunk_paths_between(audio_dir, chunk_seconds, start, end):
    """Archived WAV chunks overlapping the wall-clock interval [start, end]."""
    paths = []
    for name in sorted(os.listdir(audio_dir)):
        if not (name.startswith("chunk_") and name.endswith(".wav")):
            continue
        try:
            t = datetime.strptime(name[6:-4], "%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            continue
        if t <= end and t + chunk_seconds + 1 >= start:
            paths.append(os.path.abspath(os.path.join(audio_dir, name)))
    return paths


class Recorder:
    def __init__(self, args, cfg):
        self.args = args
        self.cfg = cfg
        self.data_dir = args.data_dir
        self.audio_dir = os.path.join(self.data_dir, "audio")
        os.makedirs(self.audio_dir, exist_ok=True)
        self.blocks = BlockLog(os.path.join(self.data_dir, "blocks.jsonl"))
        self.status_path = os.path.join(self.data_dir, "status.json")
        self.stop = threading.Event()
        self.proc = None
        connect = (soniox_stream.make_fake_connect(args.fake_drop_every) if args.fake
                   else soniox_stream.make_connect(cfg))
        self.streamer = soniox_stream.SonioxStreamer(
            cfg, self.blocks,
            lambda start, end: chunk_paths_between(self.audio_dir, args.chunk_seconds, start, end),
            connect,
        )
        self.status = {
            "recording": False,
            "pid": os.getpid(),
            "source": args.file or args.url or f"device {args.device}",
            "provider": "soniox",
            "backend": ("fake " if args.fake else "") + cfg.model,
            "last_chunk_at": None,
            "started_at": now_iso(),
            "heartbeat": now_iso(),
            "ffmpeg_restarts": 0,
            "last_error": None,
        }

    def heartbeat_loop(self):
        while not self.stop.is_set():
            self.write_status()
            self.stop.wait(2)

    def write_status(self, **changes):
        self.status.update(self.streamer.status())
        self.status.update(changes, heartbeat=now_iso())
        try:
            write_json_atomic(self.status_path, self.status)
        except OSError as e:
            log(f"could not write status: {e}")

    def on_chunk(self, filename):
        self.status["last_chunk_at"] = now_iso()
        log(f"chunk {filename} archived")

    def run_ffmpeg_once(self):
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        list_path = os.path.join(self.audio_dir, f"segments_{run_id}.csv")
        cmd = ffmpeg_command(self.args, self.audio_dir, list_path)
        log("starting ffmpeg: " + " ".join(cmd))
        # A separate Windows process group lets Ctrl+Break close FFmpeg's WAV
        # output cleanly without interrupting the recorder's final token drain.
        process_options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                           if sys.platform == "win32" else {})
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     **process_options)
        reader = threading.Thread(
            target=soniox_stream.pcm_reader, args=(self.proc.stdout, self.streamer), daemon=True
        )
        reader.start()
        self.write_status(recording=True)
        seen = 0
        while True:
            exited = self.proc.poll() is not None
            # The segment list gets a line only once a chunk file is complete.
            if os.path.exists(list_path):
                with open(list_path, newline="", encoding="utf-8") as f:
                    rows = [r for r in csv.reader(f) if r]
                for row in rows[seen:]:
                    self.on_chunk(row[0])
                seen = len(rows)
            if exited:
                break
            time.sleep(0.5)
        code = self.proc.returncode
        reader.join(timeout=5)
        self.write_status(recording=False)
        return code

    def run(self):
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        log(f"data dir: {os.path.abspath(self.data_dir)}")
        self.streamer.start()
        while not self.stop.is_set():
            code = self.run_ffmpeg_once()
            if self.stop.is_set() or self.args.file:
                break
            msg = f"ffmpeg exited with code {code}, restarting in 2s"
            log(msg)
            self.write_status(last_error=msg, ffmpeg_restarts=self.status["ffmpeg_restarts"] + 1)
            self.stop.wait(2)
        log("capture stopped, waiting for the last tokens")
        self.streamer.finish(timeout=90)
        self.stop.set()
        self.write_status(recording=False)
        log("done")

    def request_stop(self, *_):
        if self.stop.is_set():
            return
        log("stopping (Ctrl+C again is not needed, finishing the current chunk)")
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            # Both signals let ffmpeg close the current chunk cleanly.
            self.proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32"
                                  else signal.SIGINT)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(here, ".env"))

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--url", help="stream URL ffmpeg can read (HLS .m3u8, http, rtmp, ...)")
    src.add_argument("--device", help='Windows DirectShow audio device name, e.g. "CABLE Output (VB-Audio Virtual Cable)"; '
                     'on macOS, AVFoundation input, e.g. ":BlackHole 2ch"')
    src.add_argument("--file", help="pre-recorded audio/video file, played back in real time (rehearsal)")
    p.add_argument("--data-dir", default=os.path.join(here, "data", "live"))
    p.add_argument("--chunk-seconds", type=int, default=15, help="length of the archived WAV chunks")
    p.add_argument("--fake", action="store_true", help="no Soniox calls, emit sample text (UI rehearsal)")
    p.add_argument("--fake-drop-every", type=int, default=0,
                   help="with --fake, drop the connection every N seconds, then refuse reconnects for 8 s")
    p.add_argument("--context-file", help="context file (default: contexto.txt if present); "
                   "'key: value' lines are general info, other lines are terms")
    p.add_argument("--check", action="store_true", help="check key, network and a real round-trip, then exit")
    args = p.parse_args()

    region = os.environ.get("SONIOX_REGION", "us").strip().lower()
    if region not in soniox_stream.REGION_HOSTS:
        sys.exit(f"SONIOX_REGION must be one of {sorted(soniox_stream.REGION_HOSTS)}")
    cfg = argparse.Namespace(
        api_key=os.environ.get("SONIOX_API_KEY", ""),
        model=os.environ.get("SONIOX_MODEL", soniox_stream.DEFAULT_MODEL),
        region=region,
    )
    # contexto.txt next to the recorder is used automatically unless --context-file is given.
    context_file = args.context_file or os.path.join(here, "contexto.txt")
    if args.context_file and not os.path.isfile(args.context_file):
        sys.exit(f"context file not found: {args.context_file}")
    if os.path.isfile(context_file):
        cfg.extra_general, cfg.extra_terms = soniox_stream.load_context(context_file)
        cfg.context_source = context_file
    else:
        cfg.extra_general, cfg.extra_terms = {}, []
        cfg.context_source = "built-in defaults only"
    size = len(json.dumps(soniox_stream.build_context(cfg), ensure_ascii=False))
    if size > soniox_stream.CONTEXT_MAX_CHARS:
        sys.exit(f"context is {size} characters; Soniox accepts about {soniox_stream.CONTEXT_MAX_CHARS}. Shorten {context_file}.")
    if not args.fake and not cfg.api_key:
        sys.exit("SONIOX_API_KEY is not set (export it or put it in .env). Use --fake to rehearse without it.")
    if args.check:
        sys.exit(soniox_stream.check(cfg))
    soniox_stream.describe_context(cfg)

    if not (args.url or args.device or args.file):
        p.error("one of --url, --device or --file is required")
    if args.device and sys.platform not in ("win32", "darwin"):
        p.error("--device supports Windows and macOS; use --url or --file on this OS")
    if args.file and not os.path.isfile(args.file):
        sys.exit(f"file not found: {args.file}")

    os.makedirs(args.data_dir, exist_ok=True)
    rec = Recorder(args, cfg)
    signal.signal(signal.SIGINT, rec.request_stop)
    signal.signal(signal.SIGTERM, rec.request_stop)
    rec.run()


if __name__ == "__main__":
    main()
