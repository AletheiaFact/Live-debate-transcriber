#!/usr/bin/env python3
"""Local operator UI for the live transcription.

Runs separately from recorder.py and only reads its files, except
labels.json, edits.json and outbox.jsonl, which only this server writes.
Text edits are kept apart from blocks.jsonl and applied on top of it. Standard library only.

If the Aletheia platform is configured in .env (see platform_client.py),
"send speech" queues labelled blocks for the platform; sending happens in a
background thread and never touches the recorder.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import platform_client

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "static", "index.html")
VALID_LABELS = {"1", "2", "M"}
HEARTBEAT_STALE_SECONDS = 10


def to_epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


class Store:
    def __init__(self, data_dir):
        self.blocks_path = os.path.join(data_dir, "blocks.jsonl")
        self.labels_path = os.path.join(data_dir, "labels.json")
        self.edits_path = os.path.join(data_dir, "edits.json")
        self.status_path = os.path.join(data_dir, "status.json")
        self.lock = threading.Lock()
        self.offset = 0
        self.merged = {}
        self.labels = self._read_json(self.labels_path) or {}
        self.edits = self._read_json(self.edits_path) or {}  # block id -> operator's text

    @staticmethod
    def _read_json(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def blocks(self):
        """Blocks in arrival order; the last line per id wins. Reads only new lines."""
        try:
            size = os.path.getsize(self.blocks_path)
        except OSError:
            return []
        if size < self.offset:  # file was replaced: start over
            self.offset, self.merged = 0, {}
        if size > self.offset:
            with open(self.blocks_path, "rb") as f:
                f.seek(self.offset)
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break  # recorder is mid-write; pick it up next poll
                    self.offset += len(raw)
                    try:
                        block = json.loads(raw)
                    except ValueError:
                        continue
                    bid = block["id"]
                    self.merged[bid] = {**self.merged.get(bid, {}), **block}
        return list(self.merged.values())

    def current_blocks(self):
        """Blocks with the operator's text edits applied. Caller holds self.lock."""
        out = []
        for b in self.blocks():
            if b["id"] in self.edits:
                b = {**b, "text": self.edits[b["id"]], "edited": True, "original_text": b.get("text", "")}
            out.append(b)
        return out

    def set_edits(self, edits):
        """edits: {block id: new text, or None to restore the transcribed text}."""
        with self.lock:
            by_id = {b["id"]: b for b in self.blocks()}
            for bid in edits:
                b = by_id.get(bid)
                if b is None:
                    raise ValueError(f"bloco desconhecido: {bid}")
                if b.get("status") != "ok" or b.get("final") is False:
                    raise ValueError("só dá para editar blocos finalizados e transcritos; espere finalizar")
            for bid, text in edits.items():
                if text is None or text == by_id[bid].get("text", ""):
                    self.edits.pop(bid, None)
                else:
                    self.edits[bid] = text
            tmp = self.edits_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.edits, f, ensure_ascii=False, indent=0)
            os.replace(tmp, self.edits_path)

    def state(self):
        now = time.time()
        with self.lock:
            blocks = self.current_blocks()
            labels = dict(self.labels)
        status = self._read_json(self.status_path) or {}
        heartbeat = to_epoch(status.get("heartbeat"))
        recorder_alive = heartbeat is not None and now - heartbeat < HEARTBEAT_STALE_SECONDS

        last_chunk = status.get("last_chunk_at")
        last_ok = max((b.get("transcribed_at", "") for b in blocks if b.get("status") == "ok"), default=None)
        # The alarm counts silence since the last Soniox token, or since the
        # recorder started when no token has arrived yet.
        since = to_epoch(status.get("last_token_at")) or to_epoch(status.get("started_at"))
        return {
            "now": datetime.now().astimezone().isoformat(timespec="seconds"),
            "blocks": blocks,
            "labels": labels,
            "status": {
                "recording": bool(status.get("recording")) and recorder_alive,
                "recorder_alive": recorder_alive,
                "source": status.get("source"),
                "backend": status.get("backend"),
                "last_error": status.get("last_error"),
                "ffmpeg_restarts": status.get("ffmpeg_restarts", 0),
                "last_chunk_at": last_chunk,
                "last_ok_at": last_ok,
                "seconds_since_chunk": round(now - since) if since else None,
                "ws_state": status.get("ws_state"),
                "ws_error": status.get("ws_error"),
                "backlog_s": status.get("backlog_s"),
                "reconnects": status.get("reconnects", 0),
                "failed": sum(b.get("status") == "failed" for b in blocks),
            },
        }

    def set_label(self, block_id, label):
        with self.lock:
            if label:
                self.labels[block_id] = label
            else:
                self.labels.pop(block_id, None)
            tmp = self.labels_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.labels, f, ensure_ascii=False, indent=0)
            os.replace(tmp, self.labels_path)


class Handler(BaseHTTPRequestHandler):
    store = None
    outbox = None

    def log_message(self, fmt, *args):
        pass  # polling every 2s would flood the terminal

    def _host_ok(self):
        # Reject DNS-rebinding requests that reach us under another hostname.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host in ("127.0.0.1", "localhost", "[::1]"):
            return True
        self.send_error(403)
        return False

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, data):
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        if path == "/":
            with open(INDEX, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/state":
            state = self.store.state()
            state["platform"] = self.outbox.state()
            self._json(200, state)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._host_ok():
            return
        if self.path not in ("/api/label", "/api/send", "/api/edit"):
            return self.send_error(404)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            return self.send_error(413)
        if self.path == "/api/send":
            return self._send_speech(length)
        if self.path == "/api/edit":
            return self._edit(length)
        try:
            body = json.loads(self.rfile.read(length))
            block_id, label = str(body["id"]), body.get("label") or ""
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "esperado {id, label}"})
        if label and label not in VALID_LABELS:
            return self._json(400, {"error": f"o rótulo deve ser um de {sorted(VALID_LABELS)} ou vazio"})
        self.store.set_label(block_id, label)
        self._json(200, {"ok": True})

    def _edit(self, length):
        """{"edits": {block id: text or null}}; several blocks at once for "move text"."""
        try:
            edits = json.loads(self.rfile.read(length))["edits"]
            if not isinstance(edits, dict) or not edits:
                raise TypeError
            edits = {str(k): (None if v is None else " ".join(str(v).split())) for k, v in edits.items()}
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "esperado {edits: {blockId: texto ou null}}"})
        try:
            self.store.set_edits(edits)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        self._json(200, {"ok": True})

    def _send_speech(self, length):
        """Queue blocks labelled 1/2 for the platform; live blocks wait until final."""
        if self.outbox.cfg.missing():
            return self._json(400, {"error": "plataforma não configurada: " + ", ".join(self.outbox.cfg.missing())})
        try:
            ids = [str(i) for i in json.loads(self.rfile.read(length))["blockIds"]]
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "esperado {blockIds: [...]}"})
        state = self.store.state()
        by_id = {b["id"]: b for b in state["blocks"]}
        if not ids or len(set(ids)) != len(ids) or any(i not in by_id for i in ids):
            return self._json(400, {"error": "bloco desconhecido ou repetido"})
        wanted = set(ids)
        blocks = [b for b in state["blocks"] if b["id"] in wanted]  # debate order
        labels = {state["labels"].get(i) for i in ids}
        if len(labels) != 1 or not labels <= {"1", "2"}:
            return self._json(400, {"error": "rotule o bloco com 1 (Lula) ou 2 (Flávio) antes"})
        if any(b.get("status") != "ok" for b in blocks):
            return self._json(400, {"error": "um bloco com falha não pode ser enviado"})
        live = any(not _is_final(b) for b in blocks)
        if not live and any(not b.get("text") for b in blocks):
            return self._json(400, {"error": "o bloco não tem texto"})
        item = self.outbox.enqueue(labels.pop(), blocks, waiting=live)
        self._json(200, {"externalId": item["externalId"], "status": item["status"]})


def _is_final(block):
    return block.get("status") == "ok" and block.get("final") is not False


def resolve_blocks(store, ids):
    """For the outbox: current blocks once all are final, None while live, [] if unusable."""
    with store.lock:
        by_id = {b["id"]: b for b in store.current_blocks()}
    blocks = [by_id.get(i) for i in ids]
    if None in blocks:
        return []
    if any(b.get("final") is False for b in blocks):
        return None
    if any(b.get("status") != "ok" or not b.get("text") for b in blocks):
        return []
    return blocks


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default=os.path.join(HERE, "data", "live"))
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    platform_client.load_dotenv()
    Handler.store = Store(args.data_dir)
    store = Handler.store
    Handler.outbox = platform_client.Outbox(
        platform_client.Config(), args.data_dir, resolve=lambda ids: resolve_blocks(store, ids)
    )
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"cannot listen on 127.0.0.1:{args.port}: {e}")
    print(f"UI on http://127.0.0.1:{args.port}  (data: {os.path.abspath(args.data_dir)})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
