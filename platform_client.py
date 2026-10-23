#!/usr/bin/env python3
"""Send labelled speeches to the Aletheia platform as debate transcript drafts.

Uses PR AletheiaFact/aletheia#2655:
POST /api/claim/debate/:debateId/append with
{"turns": [{personality, text, externalId, startedAt}], "isLive": true}.
A turn whose externalId is already on the debate is skipped by the platform,
so resending is safe. With ALETHEIA_SEND_CHECKABILITY=1 each turn also carries
"checkability" (0-100) when the block has a current score; leave it off until
the platform accepts the field.

Auth: each operator logs in with their own fact-checker account through
Ory's native (API) login flow. The resulting session token is sent as
X-Session-Token, which the platform's SessionGuard accepts.

    python3 platform_client.py login    # asks e-mail and password, saves the token
    python3 platform_client.py whoami   # checks the saved token against the platform

Standard library only.
"""

import argparse
import getpass
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(HERE, ".aletheia_session.json")
USER_AGENT = "debate-transcriber/1.0"
LABEL_TO_ENV = {"1": "ALETHEIA_PERSONALITY_LULA", "2": "ALETHEIA_PERSONALITY_FLAVIO"}
RETRY_DELAYS = [2, 5, 10, 30, 60]


def load_dotenv(path=os.path.join(HERE, ".env")):
    """Minimal .env loader: KEY=value lines, existing env vars win."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip().removeprefix("export ").strip(), value.strip().strip("'\""))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Config:
    def __init__(self):
        self.aletheia_url = os.environ.get("ALETHEIA_URL", "").rstrip("/")
        self.ory_url = os.environ.get("ORY_URL", "").rstrip("/")
        self.debate_id = os.environ.get("ALETHEIA_DEBATE_ID", "")
        self.personalities = {label: os.environ.get(env, "") for label, env in LABEL_TO_ENV.items()}
        # Part of every externalId, so it must differ between the 3-4 machines.
        self.machine = os.environ.get("ALETHEIA_MACHINE") or f"{getpass.getuser()}-{socket.gethostname().split('.')[0]}"
        self.send_checkability = os.environ.get("ALETHEIA_SEND_CHECKABILITY", "").strip().lower() in ("1", "true", "yes")

    def missing(self):
        names = {"ALETHEIA_URL": self.aletheia_url, "ORY_URL": self.ory_url, "ALETHEIA_DEBATE_ID": self.debate_id}
        names.update({env: self.personalities[label] for label, env in LABEL_TO_ENV.items()})
        return [k for k, v in names.items() if not v]


def http_json(method, url, body=None, headers=None, timeout=15):
    """Returns (status, parsed JSON or text). Network errors raise OSError."""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        **({"Content-Type": "application/json"} if data is not None else {}),
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text


# --- session ---------------------------------------------------------------------------

def read_session():
    try:
        with open(SESSION_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_session(data):
    tmp = SESSION_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, SESSION_PATH)


def ory_messages(body):
    """Collect the human-readable messages from an Ory flow response."""
    if not isinstance(body, dict):
        return str(body)[:300]
    msgs = [m.get("text", "") for m in (body.get("ui") or {}).get("messages") or []]
    for node in (body.get("ui") or {}).get("nodes") or []:
        msgs += [m.get("text", "") for m in node.get("messages") or []]
    if body.get("error"):
        err = body["error"]
        msgs.append(err.get("reason") or err.get("message", "") if isinstance(err, dict) else str(err))
    return "; ".join(m for m in msgs if m) or json.dumps(body)[:300]


def login(cfg, email=None):
    email = email or input("E-mail: ").strip()
    password = getpass.getpass("Senha (não aparece na tela): ")
    status, flow = http_json("GET", f"{cfg.ory_url}/self-service/login/api")
    if status != 200 or not isinstance(flow, dict) or "id" not in flow:
        sys.exit(f"Não consegui abrir o fluxo de login no Ory (HTTP {status}): {ory_messages(flow)}")
    action = flow["ui"]["action"]
    status, body = http_json("POST", action, {"method": "password", "identifier": email, "password": password})
    password = None
    if status != 200 or not isinstance(body, dict) or not body.get("session_token"):
        sys.exit(f"Login recusado (HTTP {status}): {ory_messages(body)}")
    session = body.get("session") or {}
    if session.get("authenticator_assurance_level") == "aal1" and any(
        c.get("action") == "show_settings_ui" for c in body.get("continue_with") or []
    ):
        print("Aviso: o Ory pediu um passo extra de configuração; se os envios falharem, logue pelo site uma vez.")
    traits = (session.get("identity") or {}).get("traits") or {}
    save_session({
        "token": body["session_token"],
        "email": traits.get("email", email),
        "name": traits.get("name") or traits.get("email", email),
        "expires_at": session.get("expires_at"),
        "logged_in_at": now_iso(),
    })
    print(f"Login OK como {traits.get('email', email)}; sessão expira em {session.get('expires_at')}.")
    return whoami(cfg)


def whoami(cfg):
    sess = read_session()
    if not sess:
        print("Nenhuma sessão salva. Rode: python3 platform_client.py login")
        return 1
    print(f"Sessão: {sess.get('email')} (expira {sess.get('expires_at')})")
    status, body = http_json(
        "GET", f"{cfg.aletheia_url}/api/claim/debate/{cfg.debate_id}/transcript-turns",
        headers={"X-Session-Token": sess["token"]},
    )
    if status == 200:
        n = len(body) if isinstance(body, list) else len((body or {}).get("turns", []))
        print(f"OK: a plataforma aceitou a sessão; o debate tem {n} turno(s) de transcrição.")
        return 0
    print(f"FALHA: HTTP {status} ao listar os turnos do debate: {str(body)[:300]}")
    if status in (401, 403):
        print("A conta precisa ser fact-checker ou superior, e a sessão precisa estar válida.")
    elif status == 404:
        print("Debate não encontrado: ALETHEIA_DEBATE_ID deve ser o contentId do debate, não o id do claim.")
    return 1


def _speech_text(node):
    """Join the sentence texts of a parsed speech (nested `content` lists)."""
    if isinstance(node, dict):
        inner = node.get("content")
        return _speech_text(inner) if isinstance(inner, list) else (inner if isinstance(inner, str) else "")
    if isinstance(node, list):
        return " ".join(t for t in (_speech_text(n) for n in node) if t)
    return ""


def list_turns(cfg, status=None):
    sess = read_session()
    if not sess:
        print("Nenhuma sessão salva. Rode: python3 platform_client.py login")
        return 1
    url = f"{cfg.aletheia_url}/api/claim/debate/{cfg.debate_id}/transcript-turns"
    status_code, body = http_json("GET", url + (f"?status={status}" if status else ""),
                                  headers={"X-Session-Token": sess["token"]})
    if status_code != 200 or not isinstance(body, list):
        print(f"FALHA: HTTP {status_code}: {str(body)[:300]}")
        return 1
    names = {v: k for k, v in cfg.personalities.items()}
    who = {"1": "Lula", "2": "Flávio"}
    print(f"{len(body)} turno(s){' com status ' + status if status else ''}:\n")
    for t in body:
        speaker = who.get(names.get(str(t.get("personality"))), str(t.get("personality")))
        print(f"[{t.get('status')}] turnId={t.get('_id')}  {speaker}  externalId={t.get('externalId')}")
        print(f"    {_speech_text(t.get('speech')) or '(sem texto)'}\n")
    return 0


def review(cfg, action, turn_ids):
    sess = read_session()
    if not sess:
        print("Nenhuma sessão salva. Rode: python3 platform_client.py login")
        return 1
    url = f"{cfg.aletheia_url}/api/claim/debate/{cfg.debate_id}/transcript-turns/{action}"
    status, body = http_json("POST", url, {"turnIds": turn_ids}, headers={"X-Session-Token": sess["token"]})
    print(f"HTTP {status}: {json.dumps(body, ensure_ascii=False)[:500]}")
    return 0 if status in (200, 201) else 1


# --- outbox ---------------------------------------------------------------------------------

class Outbox:
    """Persistent queue of blocks to send. One worker thread; never blocks the UI.

    Items live in <data-dir>/outbox.jsonl (last line per externalId wins).
    Statuses: waiting (block still live; `resolve` is polled until it is final)
    -> queued -> sent | skipped (already on the debate) | failed (rejected, will
    not retry); network errors and 5xx/409/429 keep it queued with backoff;
    401/403 keep it queued as "auth" until the operator logs in again.

    `resolve(block_ids)` returns the current blocks when all are final, None
    while any is still live, or [] when a block is gone or ended empty.
    """

    def __init__(self, cfg, data_dir, resolve=None):
        self.cfg = cfg
        self.resolve = resolve
        self.path = os.path.join(data_dir, "outbox.jsonl")
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.items = {}
        self.last_error = None
        self.last_ok_at = None
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    self.items[item["externalId"]] = item
        for item in self.items.values():
            if item["status"] in ("sending", "error", "auth"):
                item["status"], item["next_try"] = "queued", 0
        threading.Thread(target=self._run, daemon=True, name="outbox").start()

    def _persist(self, item):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def enqueue(self, label, blocks, waiting=False, checkability=""):
        # The label is part of the id: relabelling a sent block and sending it
        # again creates a corrected draft instead of being skipped.
        if len(blocks) == 1:
            external_id = f"{self.cfg.machine}:{blocks[0]['id']}:{label}"
            if blocks[0].get("edited"):
                # An edited text is a new version: it must not be skipped as already sent.
                external_id += ":e" + hashlib.sha1(blocks[0]["text"].encode()).hexdigest()[:6]
        else:
            # Hash of every block id, so sets with the same ends do not collide.
            digest = hashlib.sha1("|".join(b["id"] for b in blocks).encode()).hexdigest()[:8]
            external_id = f"{self.cfg.machine}:{blocks[0]['id']}..{blocks[-1]['id']}:{label}:{digest}"
        with self.lock:
            existing = self.items.get(external_id)
            if existing and existing["status"] != "failed":
                return existing
            item = {
                "externalId": external_id,
                "label": label,
                "personality": self.cfg.personalities[label],
                "blockIds": [b["id"] for b in blocks],
                "text": None if waiting else " ".join(b["text"].strip() for b in blocks),
                "startedAt": blocks[0]["start"],
                "checkability": checkability,
                "status": "waiting" if waiting else "queued",
                "attempts": 0,
                "next_try": 0,
                "created_at": now_iso(),
                "error": None,
            }
            self.items[external_id] = item
            self._persist(item)
        self.wake.set()
        return item

    def state(self):
        sess = read_session()
        with self.lock:
            items = [dict(i) for i in self.items.values()]
        counts = {}
        for i in items:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
        return {
            "target": "platform",
            "configured": not self.cfg.missing(),
            "missing": self.cfg.missing(),
            "logged_in_as": sess.get("email") if sess else None,
            "session_expires_at": sess.get("expires_at") if sess else None,
            "counts": counts,
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
            "items": [{k: i[k] for k in ("externalId", "label", "blockIds", "status", "error", "turnId", "text")
                       if k in i} for i in items],
        }

    def _due(self):
        now = time.time()
        with self.lock:
            due = [i for i in self.items.values() if i["status"] in ("queued", "error", "auth") and i["next_try"] <= now]
        return min(due, key=lambda i: i["created_at"]) if due else None

    def _promote_waiting(self):
        with self.lock:
            waiting = [i for i in self.items.values() if i["status"] == "waiting"]
        for item in waiting:
            blocks = self.resolve(item["blockIds"]) if self.resolve else []
            if blocks is None:
                continue  # still live
            if not blocks:
                self._update(item, status="failed", error="o bloco ficou vazio ou sumiu")
                continue
            self._update(item, status="queued", next_try=0,
                         text=" ".join(b["text"].strip() for b in blocks), startedAt=blocks[0]["start"])

    def _run(self):
        while True:
            self._promote_waiting()
            item = self._due()
            if item is None:
                self.wake.wait(1)
                self.wake.clear()
                continue
            self._send(item)

    def _update(self, item, **changes):
        with self.lock:
            item.update(changes, updated_at=now_iso())
            self._persist(item)

    def _retry_later(self, item, status, error):
        delay = RETRY_DELAYS[min(item["attempts"], len(RETRY_DELAYS) - 1)]
        self.last_error = error
        self._update(item, status=status, error=error, attempts=item["attempts"] + 1,
                     next_try=time.time() + delay)

    def _send(self, item):
        sess = read_session()  # re-read each time, so a new login applies without a restart
        if not sess:
            return self._retry_later(item, "auth", "não logado: rode python3 platform_client.py login")
        turn = {"personality": item["personality"], "text": item["text"],
                "externalId": item["externalId"], "startedAt": item["startedAt"]}
        if self.cfg.send_checkability and item.get("checkability") not in (None, ""):
            turn["checkability"] = item["checkability"]
        url = f"{self.cfg.aletheia_url}/api/claim/debate/{self.cfg.debate_id}/append"
        try:
            status, body = http_json("POST", url, {"turns": [turn], "isLive": True},
                                     headers={"X-Session-Token": sess["token"]})
        except OSError as e:
            return self._retry_later(item, "error", f"rede: {e}")
        if status in (200, 201) and isinstance(body, dict):
            self.last_error, self.last_ok_at = None, now_iso()
            if item["externalId"] in (body.get("skipped") or []):
                return self._update(item, status="skipped", error=None)
            draft = next((d for d in body.get("drafts") or [] if d.get("externalId") == item["externalId"]), {})
            return self._update(item, status="sent", error=None, turnId=draft.get("turnId"))
        detail = f"HTTP {status}: {str(body)[:200]}"
        if status in (401, 403):
            return self._retry_later(item, "auth", f"{detail} (faça login de novo)")
        if status in (408, 409, 429) or status >= 500:
            return self._retry_later(item, "error", detail)
        self.last_error = detail
        self._update(item, status="failed", error=detail)


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["login", "whoami", "turns", "approve", "reject"])
    p.add_argument("turn_ids", nargs="*", help="approve/reject: turnIds (from 'turns')")
    p.add_argument("--status", choices=["draft", "approved", "rejected"], help="turns: filter by status")
    p.add_argument("--email", default=os.environ.get("ALETHEIA_EMAIL"))
    args = p.parse_args()
    cfg = Config()
    if cfg.missing():
        sys.exit("Faltam variáveis no .env: " + ", ".join(cfg.missing()))
    if args.command == "login":
        sys.exit(login(cfg, args.email))
    if args.command == "turns":
        sys.exit(list_turns(cfg, args.status))
    if args.command in ("approve", "reject"):
        if not args.turn_ids:
            sys.exit(f"usage: python3 platform_client.py {args.command} <turnId> [<turnId> ...]")
        sys.exit(review(cfg, args.command, args.turn_ids))
    sys.exit(whoami(cfg))


if __name__ == "__main__":
    main()
