#!/usr/bin/env python3
"""Local operator UI for the live transcription.

Runs separately from recorder.py and only reads its files, except
labels.json, edits.json, operator.json and send queues, which only this server writes.
Checkability scores are read from checkability.jsonl, written by checkability.py.
Text edits are kept apart from blocks.jsonl and applied on top of it.

SEND_TARGET selects Google Sheets, the Aletheia platform, or both
(sheets,platform) in .env. Sending happens in background threads and never
touches the recorder.
"""

import argparse
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import platform_client
import sheets_client

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
        self.operator_path = os.path.join(data_dir, "operator.json")
        self.scores_path = os.path.join(data_dir, "checkability.jsonl")
        self.scores_offset = 0
        self.scores = {}
        self.operator = self._read_json(self.operator_path) or {"settings": {}, "groups": {}}
        self.lock = threading.Lock()
        self.offset = 0
        self.merged = {}
        self.labels = self._read_json(self.labels_path) or {}
        self.edits = self._read_json(self.edits_path) or {}  # block id -> operator's text
        self.send_locked = lambda bid: False

    def require_mutable(self, *ids):
        if any(self.send_locked(bid) for bid in ids):
            raise ValueError("bloco em envio ou enviado à planilha; faça correções na planilha após o envio")

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

    def checkability(self, blocks):
        """Latest score per current block, read incrementally from checkability.jsonl.

        stale: the block's text changed since it was scored and checkability.py
        has not rescored it yet.
        """
        try:
            size = os.path.getsize(self.scores_path)
        except OSError:
            size = 0
        if size < self.scores_offset:  # file was replaced: start over
            self.scores_offset, self.scores = 0, {}
        if size > self.scores_offset:
            with open(self.scores_path, "rb") as f:
                f.seek(self.scores_offset)
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break  # checkability.py is mid-write; pick it up next poll
                    self.scores_offset += len(raw)
                    try:
                        item = json.loads(raw)
                        self.scores[item["block_id"]] = item
                    except (ValueError, KeyError):
                        continue
        out = {}
        for b in blocks:
            item = self.scores.get(b["id"])
            if item:
                scored_text = (item.get("state") or {}).get("target_block")
                out[b["id"]] = {k: item.get(k) for k in ("percentage", "method", "model", "rubric_version")}
                out[b["id"]]["stale"] = scored_text != " ".join(b.get("text", "").split())
        return out

    def current_blocks(self):
        """Blocks with the operator's text edits applied. Caller holds self.lock."""
        raw = self.blocks()
        by_id = {b["id"]: b for b in raw}
        groups = self.operator["groups"]
        children = {bid for g in groups.values() for bid in g["children"]}
        roots = {gid: g for gid, g in groups.items() if gid not in children}
        hidden = {bid for g in roots.values() for bid in g["source_ids"]}
        at = {g["source_ids"][0]: gid for gid, g in roots.items()}
        out = []
        for b in raw:
            gid = at.get(b["id"])
            if gid:
                g = groups[gid]
                sources = [by_id[bid] for bid in g["source_ids"]]
                original = " ".join(s.get("text", "").strip() for s in sources).strip()
                b = {**sources[0], "id": gid, "text": g["baseline"],
                     "end_ms": sources[-1].get("end_ms"), "source_ids": g["source_ids"],
                     "original_text": original, "edited": g["baseline"] != original,
                     "merged": True, "merged_edited": gid in self.edits}
            elif b["id"] in hidden:
                continue
            if b["id"] in self.edits:
                original = b.get("original_text", b.get("text", ""))
                b = {**b, "text": self.edits[b["id"]], "edited": self.edits[b["id"]] != original,
                     "original_text": original}
            out.append(b)
        return out

    @staticmethod
    def _save(path, value):
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(path + ".tmp", path)

    def set_settings(self, settings):
        start = settings.get("debate_start", "")
        if start:
            datetime.fromisoformat(start)  # require a complete date and explicit timezone
            if "T" not in start or datetime.fromisoformat(start).tzinfo is None:
                raise ValueError("informe a data, o horário e o fuso do início")
        if not isinstance(settings.get("show_minutage"), bool):
            raise ValueError("opção de minutagem inválida")
        with self.lock:
            self.operator["settings"] = {"debate_start": start, "show_minutage": settings["show_minutage"]}
            self._save(self.operator_path, self.operator)

    def merge_down(self, bid):
        with self.lock:
            blocks = [b for b in self.current_blocks()
                      if not (b.get("final") is True and b.get("status") == "ok" and not b.get("text") and not b.get("edited"))]
            index = next((i for i, b in enumerate(blocks) if b["id"] == bid), -1)
            if index < 0 or index + 1 >= len(blocks):
                raise ValueError("não há próximo bloco")
            pair = blocks[index:index + 2]
            self.require_mutable(*(b["id"] for b in pair))
            if any(not _is_final(b) for b in pair):
                raise ValueError("espere os dois blocos finalizarem")
            labels = {self.labels.get(b["id"]) for b in pair} - {None, ""}
            if len(labels) > 1:
                raise ValueError("os blocos têm falantes diferentes")
            gid = "merge-" + uuid.uuid4().hex
            self.operator["groups"][gid] = {
                "children": [b["id"] for b in pair],
                "source_ids": [sid for b in pair for sid in b.get("source_ids", [b["id"]])],
                "baseline": " ".join(b["text"].strip() for b in pair).strip(),
            }
            if labels:
                self.labels[gid] = labels.pop()
            self._save(self.labels_path, self.labels)
            self._save(self.operator_path, self.operator)
            return gid

    def unmerge(self, bid, confirmed=False):
        with self.lock:
            self.require_mutable(bid)
            b = next((b for b in self.current_blocks() if b["id"] == bid), None)
            if not b or not b.get("merged"):
                raise ValueError("bloco combinado desconhecido")
            if b["merged_edited"] and not confirmed:
                raise ValueError("confirme o descarte das edições feitas após combinar")
            first = self.operator["groups"][bid]["children"][0]
            del self.operator["groups"][bid]
            self.edits.pop(bid, None)
            self.labels.pop(bid, None)
            self._save(self.operator_path, self.operator)
            self._save(self.edits_path, self.edits)
            self._save(self.labels_path, self.labels)
            return first

    def set_edits(self, edits):
        """edits: {block id: new text, or None to restore the transcribed text}."""
        with self.lock:
            self.require_mutable(*edits)
            by_id = {b["id"]: b for b in self.current_blocks()}
            for bid in edits:
                b = by_id.get(bid)
                if b is None:
                    raise ValueError(f"bloco desconhecido: {bid}")
                if b.get("status") != "ok" or b.get("final") is False:
                    raise ValueError("só dá para editar blocos finalizados e transcritos; espere finalizar")
            for bid, text in edits.items():
                if by_id[bid].get("merged"):
                    self.edits[bid] = by_id[bid]["original_text"] if text is None else text
                    continue
                if text is None or text == by_id[bid].get("original_text", by_id[bid].get("text", "")):
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
            settings = dict(self.operator["settings"])
            checkability = self.checkability(blocks)
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
            "checkability": checkability,
            "settings": settings,
            "session_start": status.get("started_at") or (blocks[0]["start"] if blocks else None),
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
            self.require_mutable(block_id)
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
    outbox = None  # primary destination: drives block locks and per-block status in the UI
    extra_outboxes = []  # further destinations that receive every send too

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
            state["extra_destinations"] = [o.state() for o in self.extra_outboxes]
            self._json(200, state)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._host_ok():
            return
        if self.path not in ("/api/label", "/api/send", "/api/edit", "/api/settings", "/api/merge", "/api/unmerge"):
            return self.send_error(404)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            return self.send_error(413)
        if self.path in ("/api/settings", "/api/merge", "/api/unmerge"):
            try:
                body = json.loads(self.rfile.read(length))
                if self.path == "/api/settings":
                    self.store.set_settings(body)
                    result = None
                elif self.path == "/api/merge":
                    result = self.store.merge_down(str(body["id"]))
                else:
                    result = self.store.unmerge(str(body["id"]), body.get("confirmed") is True)
                return self._json(200, {"ok": True, "id": result})
            except (ValueError, KeyError, TypeError) as e:
                return self._json(400, {"error": str(e)})
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
        try:
            self.store.set_label(block_id, label)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
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
        """Queue only finalized blocks labelled 1/2."""
        missing = [m for o in [self.outbox, *self.extra_outboxes] for m in o.cfg.missing()]
        if missing:
            return self._json(400, {"error": "destino não configurado: " + ", ".join(missing)})
        try:
            ids = json.loads(self.rfile.read(length))["blockIds"]
            if not isinstance(ids, list):
                raise TypeError
            ids = [str(i) for i in ids]
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "esperado {blockIds: [...]}"})
        # Snapshot validation and queue persistence share the edit lock, so an edit,
        # relabel, merge or second click cannot slip between validation and locking.
        with self.store.lock:
            current = self.store.current_blocks()
            by_id = {b["id"]: b for b in current}
            if not ids or len(set(ids)) != len(ids) or any(i not in by_id for i in ids):
                return self._json(400, {"error": "bloco desconhecido ou repetido"})
            wanted = set(ids)
            blocks = [b for b in current if b["id"] in wanted]
            labels = {self.store.labels.get(i) for i in ids}
            if len(labels) != 1 or not labels <= {"1", "2"}:
                return self._json(400, {"error": "rotule o bloco com 1 (Lula) ou 2 (Flávio) antes"})
            if any(b.get("status") != "ok" for b in blocks):
                return self._json(400, {"error": "um bloco com falha não pode ser enviado"})
            if any(not _is_final(b) for b in blocks):
                return self._json(400, {"error": "espere o bloco finalizar para enviar"})
            if any(not b.get("text", "").strip() for b in blocks):
                return self._json(400, {"error": "o bloco não tem texto"})
            label = labels.pop()
            try:
                # The primary goes first: if it rejects the block, nothing is queued anywhere.
                settings = self.store.operator["settings"]
                item = enqueue(self.outbox, label, blocks, settings)
                for outbox in self.extra_outboxes:
                    enqueue(outbox, label, blocks, settings)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        self._json(200, {"externalId": item["externalId"], "status": item["status"]})


def enqueue(outbox, label, blocks, settings):
    if isinstance(outbox, sheets_client.Outbox):
        return outbox.enqueue(label, blocks, settings)
    # Platform ids are deterministic, so a resend of a queued or sent block is a no-op there.
    return outbox.enqueue(label, blocks, waiting=False)


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
    target = os.environ.get("SEND_TARGET") or ("sheets" if os.environ.get("SPREADSHEET_ID") else "platform")
    targets = [t.strip() for t in target.split(",") if t.strip()]
    if not targets or len(set(targets)) != len(targets) or not set(targets) <= {"sheets", "platform"}:
        sys.exit("SEND_TARGET must be sheets, platform or sheets,platform")
    # Sheets snapshots are immutable, so when Sheets is a destination it is the primary.
    targets.sort(key=lambda t: t != "sheets")
    outboxes = []
    for t in targets:
        if t == "sheets":
            outboxes.append(sheets_client.Outbox(sheets_client.Config(), args.data_dir))
            store.send_locked = outboxes[-1].locked
        else:
            outboxes.append(platform_client.Outbox(
                platform_client.Config(), args.data_dir, resolve=lambda ids: resolve_blocks(store, ids)
            ))
    Handler.outbox, Handler.extra_outboxes = outboxes[0], outboxes[1:]
    print("sending to: " + ", ".join(targets), flush=True)
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
