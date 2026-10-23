#!/usr/bin/env python3
"""Append immutable transcript snapshots to Google Sheets, with delivery receipts."""

import argparse
import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime

from platform_client import load_dotenv, now_iso

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADERS = ["Início", "Fim", "Etiqueta", "Transcrição", "Time sugerido (IA)", "Status", "Colaborador"]
NAMES = {"1": "Lula", "2": "Flávio"}
RECEIPT_KEY = "ldt"
LOCKED_STATUSES = {"queued", "sending", "verifying", "sent"}


class Config:
    def __init__(self):
        self.spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
        self.credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        self.tab = os.environ.get("GOOGLE_SHEETS_TAB", "Sheet1")

    def missing(self):
        return [name for name, value in (("SPREADSHEET_ID", self.spreadsheet_id),
                                         ("GOOGLE_APPLICATION_CREDENTIALS", self.credentials_path)) if not value]


def transcript_row(label, blocks, settings):
    """Sheet timestamps are always elapsed from debate start, independent of UI display."""
    if not settings.get("debate_start"):
        raise ValueError("configure o início do debate antes de enviar à planilha")
    start = datetime.fromisoformat(settings["debate_start"])
    if start.tzinfo is None:
        raise ValueError("configure o início do debate com data e fuso antes de enviar")

    def elapsed(epoch):
        seconds = round(epoch - start.timestamp())
        n = abs(seconds)
        return ("−" if seconds < 0 else "") + f"{n // 3600:02}:{n // 60 % 60:02}:{n % 60:02}"

    first = datetime.fromisoformat(blocks[0]["start"])
    end_ms = blocks[-1].get("end_ms")
    if first.tzinfo is None or not isinstance(end_ms, (int, float)):
        raise ValueError("o bloco não tem horários completos de início e fim")
    return [elapsed(first.timestamp()), elapsed(end_ms / 1000), NAMES[label],
            " ".join(b["text"].strip() for b in blocks), "", "Novo"]


class DeliveryUnknown(Exception):
    """A write may have reached Google; keep its snapshot locked until reconciled."""


class Client:
    def __init__(self, cfg):
        self.cfg = cfg
        self.api = None

    def connect(self):
        if self.api is None:
            try:
                import httplib2
                from google.oauth2.service_account import Credentials
                from google_auth_httplib2 import AuthorizedHttp
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise ValueError("instale as dependências: python -m pip install -r requirements.txt") from exc
            credentials = Credentials.from_service_account_file(self.cfg.credentials_path, scopes=SCOPES)
            self.api = build("sheets", "v4", http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=20)),
                             cache_discovery=False).spreadsheets()
        return self.api

    def check(self):
        """Read-only check: authenticate, select the tab, and validate row 6."""
        api = self.connect()
        meta = api.get(spreadsheetId=self.cfg.spreadsheet_id,
                       fields="sheets(properties(sheetId,title))").execute(num_retries=0)
        tab = next((s["properties"] for s in meta["sheets"] if s["properties"]["title"] == self.cfg.tab), None)
        if tab is None:
            raise ValueError(f"aba não encontrada: {self.cfg.tab}")
        quoted = "'" + self.cfg.tab.replace("'", "''") + "'"
        headers = api.values().get(spreadsheetId=self.cfg.spreadsheet_id,
                                   range=quoted + "!A6:G6").execute(num_retries=0).get("values", [])
        if headers != [HEADERS]:
            raise ValueError("os cabeçalhos A6:G6 não correspondem ao modelo da transcrição")
        return tab["sheetId"]

    def receipt(self, item):
        self.connect()
        from googleapiclient.errors import HttpError
        try:
            receipt = self.connect().developerMetadata().get(
                spreadsheetId=item["spreadsheetId"], metadataId=item["metadataId"]
            ).execute(num_retries=0)
        except HttpError as exc:
            if exc.resp.status == 404:
                return False
            raise
        if receipt.get("metadataKey") != RECEIPT_KEY or receipt.get("metadataValue") != item["externalId"]:
            raise ValueError("identificador de recibo ocupado; tente enviar novamente")
        return True

    def send(self, item):
        self.connect()
        from googleapiclient.errors import HttpError
        # A persisted job keeps its original destination even if .env changes.
        if self.receipt(item):
            return
        if item["spreadsheetId"] != self.cfg.spreadsheet_id or item["tab"] != self.cfg.tab:
            raise ValueError("restaure a configuração da planilha deste envio antes de continuar")
        sheet_id = self.check()
        body = {"requests": [
            {"appendCells": {"sheetId": sheet_id,
                             "rows": [{"values": [{"userEnteredValue": {"stringValue": v}} for v in item["row"]]}],
                             "fields": "userEnteredValue"}},
            {"createDeveloperMetadata": {"developerMetadata": {
                "metadataId": item["metadataId"], "metadataKey": RECEIPT_KEY,
                "metadataValue": item["externalId"], "visibility": "DOCUMENT",
                "location": {"sheetId": sheet_id},
            }}},
        ]}
        # The row and unique receipt are one atomic operation. A repeated request
        # with the same metadataId fails as a whole, so a timeout cannot duplicate a row.
        try:
            self.api.batchUpdate(spreadsheetId=item["spreadsheetId"], body=body).execute(num_retries=0)
        except Exception as exc:
            if isinstance(exc, HttpError) and 400 <= exc.resp.status < 500 and exc.resp.status != 408:
                # Includes duplicate-receipt rejection after a previous uncertain write.
                if self.receipt(item):
                    return
                raise
            raise DeliveryUnknown(str(exc)) from exc


class Outbox:
    """One writer, durable snapshots, manual retry on failure, safe timeout recovery."""
    def __init__(self, cfg, data_dir, client=None, start_worker=True):
        self.cfg = cfg
        self.client = client or Client(cfg)
        self.path = os.path.join(data_dir, "sheets_outbox.jsonl")
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.items = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    self.items[item["externalId"]] = item
        for item in self.items.values():
            if item["status"] == "sending":
                item.update(status="verifying", uncertain=True, next_try=0)
        if start_worker:
            threading.Thread(target=self._run, daemon=True, name="sheets-outbox").start()

    def _persist(self, item):
        with open(self.path, "ab+") as f:
            f.seek(0, os.SEEK_END)
            if f.tell():
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")  # separate a torn last record after a crash
            f.write((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())

    def locked(self, bid):
        with self.lock:
            return any(bid in i["blockIds"] and i["status"] in LOCKED_STATUSES for i in self.items.values())

    def enqueue(self, label, blocks, settings):
        with self.lock:
            ids = [b["id"] for b in blocks]
            if any(set(ids).intersection(i["blockIds"]) and i["status"] in LOCKED_STATUSES
                   for i in self.items.values()):
                raise ValueError("o bloco já está em envio ou foi enviado à planilha")
            item = {"externalId": uuid.uuid4().hex[:16], "metadataId": secrets.randbelow(2**31 - 1) + 1,
                    "spreadsheetId": self.cfg.spreadsheet_id, "tab": self.cfg.tab,
                    "blockIds": ids, "label": label, "row": transcript_row(label, blocks, settings),
                    "text": " ".join(b["text"].strip() for b in blocks),
                    "status": "queued", "uncertain": False, "attempts": 0, "next_try": 0,
                    "created_at": now_iso(), "error": None}
            self._persist(item)
            self.items[item["externalId"]] = item
        self.wake.set()
        return dict(item)

    def state(self):
        with self.lock:
            items = [dict(i) for i in self.items.values()]
        counts = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {"target": "sheets", "configured": not self.cfg.missing(), "missing": self.cfg.missing(),
                "counts": counts, "items": items}

    def _update(self, item, **changes):
        with self.lock:
            updated = {**item, **changes, "updated_at": now_iso()}
            self._persist(updated)
            item.update(updated)

    def _send(self, item):
        self._update(item, status="sending")
        try:
            self.client.send(item)
        except Exception as exc:
            uncertain = item["uncertain"] or isinstance(exc, DeliveryUnknown)
            attempts = item["attempts"] + 1
            self._update(item, status="verifying" if uncertain else "failed", uncertain=uncertain,
                         attempts=attempts, next_try=time.time() + min(60, 5 * attempts), error=str(exc)[:500])
        else:
            self._update(item, status="sent", uncertain=False, error=None)

    def _run(self):
        while True:
            with self.lock:
                item = next((i for i in self.items.values() if i["status"] in {"queued", "verifying"}
                             and i["next_try"] <= time.time()), None)
            if item:
                self._send(item)
            else:
                self.wake.wait(1)
                self.wake.clear()


def main():
    argparse.ArgumentParser(description="Verifica acesso e cabeçalhos sem escrever na planilha.").parse_args()
    load_dotenv()
    cfg = Config()
    if cfg.missing():
        raise SystemExit("Faltam variáveis no .env: " + ", ".join(cfg.missing()))
    Client(cfg).check()
    print(f"OK: acesso à aba {cfg.tab}; cabeçalhos A6:G6 conferidos. Nenhum dado enviado.")


if __name__ == "__main__":
    main()
