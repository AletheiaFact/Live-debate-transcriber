"""Delivery, immutable snapshots and operator API regression tests; no network writes."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from server import Handler, Store
from sheets_client import Client, DeliveryUnknown, HEADERS, Outbox, transcript_row


def config():
    return SimpleNamespace(spreadsheet_id="test-sheet", tab="Sheet1", missing=lambda: [])


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.start = "2026-10-23T00:01:00-04:00"
        self.end_ms = int(datetime.fromisoformat("2026-10-23T00:01:07-04:00").timestamp() * 1000)
        with open(self.store.blocks_path, "w", encoding="utf-8") as f:
            for bid in ("a", "b", "c"):
                f.write(json.dumps(dict(id=bid, text="=literal claim " + bid, final=True, status="ok",
                                       start=self.start, end_ms=self.end_ms)) + "\n")
        self.store.set_settings({"debate_start": "2026-10-22T23:30:00-04:00", "show_minutage": False})
        self.store.set_label("b", "1")
        self.client = SimpleNamespace(send=lambda item: None)
        self.outbox = Outbox(config(), self.temp.name, client=self.client, start_worker=False)
        self.store.send_locked = self.outbox.locked

    def send(self, ids):
        body = json.dumps({"blockIds": ids}).encode()
        responses = []
        handler = SimpleNamespace(store=self.store, outbox=self.outbox, extra_outboxes=getattr(self, "extra", []),
                                  rfile=io.BytesIO(body), _json=lambda code, data: responses.append((code, data)))
        Handler._send_speech(handler, len(body))
        return responses[0]

    def test_elapsed_timestamps_midnight_and_column_mapping(self):
        row = transcript_row("1", [self.store.state()["blocks"][1]], self.store.operator["settings"])
        self.assertEqual(row, ["00:31:00", "00:31:07", "Lula", "=literal claim b", "Novo"])
        self.assertFalse(self.store.operator["settings"]["show_minutage"])
        with self.assertRaises(ValueError):
            transcript_row("1", [self.store.state()["blocks"][1]], {})

    def test_send_locks_all_mutations_and_adjacent_moves(self):
        self.assertEqual(self.send(["b"])[0], 200)
        for mutation in (lambda: self.store.set_label("b", "2"),
                         lambda: self.store.set_edits({"b": "changed"}),
                         lambda: self.store.set_edits({"a": "move", "b": "text"}),
                         lambda: self.store.merge_down("a"), lambda: self.store.merge_down("b")):
            with self.assertRaises(ValueError):
                mutation()
        self.assertEqual(self.store.state()["blocks"][0]["text"], "=literal claim a")
        self.assertEqual(self.send(["b"])[0], 400)

    def test_one_send_queues_both_destinations(self):
        sent = []
        missing = []
        self.extra = [SimpleNamespace(cfg=SimpleNamespace(missing=lambda: missing),
                                      enqueue=lambda label, blocks, waiting: sent.append((label, blocks)))]
        missing.append("ALETHEIA_URL")
        self.assertIn("ALETHEIA_URL", self.send(["b"])[1]["error"])
        self.assertEqual((sent, self.outbox.items), ([], {}))
        missing.clear()
        self.assertEqual(self.send(["b"])[0], 200)
        self.assertEqual([(label, [b["text"] for b in blocks]) for label, blocks in sent],
                         [("1", ["=literal claim b"])])
        self.assertEqual(self.send(["b"])[0], 400)  # Sheets rejects the resend before the platform sees it
        self.assertEqual(len(sent), 1)

    def test_success_stays_locked_after_restart(self):
        self.send(["b"])
        item = next(iter(self.outbox.items.values()))
        self.outbox._send(item)
        self.assertEqual(item["status"], "sent")
        reloaded = Outbox(config(), self.temp.name, start_worker=False)
        self.assertTrue(reloaded.locked("b"))
        self.assertEqual(reloaded.state()["counts"], {"sent": 1})

    def test_failure_unlocks_and_retry_uses_new_corrected_snapshot(self):
        def fail(item):
            raise ValueError("access denied")
        self.client.send = fail
        self.send(["b"])
        old = next(iter(self.outbox.items.values()))
        self.outbox._send(old)
        self.assertEqual(old["status"], "failed")
        self.assertFalse(self.outbox.locked("b"))
        self.store.set_edits({"b": "corrected"})
        self.assertEqual(self.send(["b"])[0], 200)
        new = list(self.outbox.items.values())[-1]
        self.assertNotEqual(new["externalId"], old["externalId"])
        self.assertEqual(new["row"][3], "corrected")
        self.assertEqual(old["row"][3], "=literal claim b")

    def test_unknown_delivery_keeps_lock_and_snapshot_until_verified(self):
        def uncertain(item):
            raise DeliveryUnknown("response lost")
        self.client.send = uncertain
        self.send(["b"])
        item = next(iter(self.outbox.items.values()))
        self.outbox._send(item)
        self.assertEqual(item["status"], "verifying")
        self.assertTrue(self.outbox.locked("b"))
        self.assertEqual(self.send(["b"])[0], 400)
        self.client.send = lambda item: None
        self.outbox._send(item)
        self.assertEqual(item["status"], "sent")
        self.assertEqual(len(self.outbox.items), 1)

    def test_interrupted_write_is_verified_after_restart(self):
        self.send(["b"])
        item = next(iter(self.outbox.items.values()))
        self.outbox._update(item, status="sending")
        reloaded = Outbox(config(), self.temp.name, start_worker=False)
        restored = reloaded.items[item["externalId"]]
        self.assertEqual(restored["status"], "verifying")
        self.assertTrue(restored["uncertain"])
        self.assertTrue(reloaded.locked("b"))

    def test_merge_snapshot_end_and_unmerge_lock(self):
        gid = self.store.merge_down("b")
        self.assertEqual(self.send([gid])[0], 200)
        row = next(iter(self.outbox.items.values()))["row"]
        self.assertEqual(row[1], "00:31:07")
        self.assertEqual(row[3], "=literal claim b =literal claim c")
        with self.assertRaises(ValueError):
            self.store.unmerge(gid, confirmed=True)

    def test_torn_last_record_does_not_corrupt_next_write(self):
        with open(self.outbox.path, "wb") as f:
            f.write(b'{"unfinished":')
        self.send(["b"])
        reloaded = Outbox(config(), self.temp.name, start_worker=False)
        self.assertTrue(reloaded.locked("b"))

    def test_http_send_state_and_mutation_rejection(self):
        class LocalHandler(Handler):
            pass
        LocalHandler.store = self.store
        LocalHandler.outbox = self.outbox
        http = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{http.server_port}"
            def post(action, body):
                request = urllib.request.Request(base + "/api/" + action, json.dumps(body).encode(),
                                                 headers={"Content-Type": "application/json"})
                try:
                    response = urllib.request.urlopen(request, timeout=3)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    return response.status, json.loads(response.read())
            self.assertEqual(post("send", {"blockIds": ["b"]})[0], 200)
            self.outbox._send(next(iter(self.outbox.items.values())))
            with urllib.request.urlopen(base + "/api/state", timeout=3) as response:
                state = json.loads(response.read())
            self.assertEqual(state["platform"]["target"], "sheets")
            self.assertEqual(state["platform"]["items"][0]["status"], "sent")
            self.assertEqual(post("label", {"id": "b", "label": "2"})[0], 400)
            self.assertEqual(post("edit", {"edits": {"b": "changed"}})[0], 400)
            self.assertEqual(post("merge", {"id": "a"})[0], 400)
            self.assertEqual(post("send", {"blockIds": ["b"]})[0], 400)
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=3)


class Request:
    def __init__(self, action):
        self.action = action

    def execute(self, num_retries):
        if num_retries != 0:
            raise AssertionError("writes must not retry outside the delivery protocol")
        return self.action()


@unittest.skipUnless(importlib.util.find_spec("googleapiclient"), "optional Google clients not installed")
class ClientTests(unittest.TestCase):
    def setUp(self):
        from googleapiclient.errors import HttpError
        def http_error(code):
            return HttpError(SimpleNamespace(status=code, reason="test"), b'{"error":{"message":"test"}}')
        self.http_error = http_error
        self.receipts = {}
        self.rows = []
        self.lose_reply = False
        self.client = Client(config())
        self.client.api = SimpleNamespace(
            get=lambda **kw: Request(lambda: {"sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]}),
            values=lambda: SimpleNamespace(get=lambda **kw: Request(lambda: {"values": [HEADERS]})),
            developerMetadata=lambda: SimpleNamespace(get=self.get_receipt),
            batchUpdate=self.batch,
        )
        self.item = {"spreadsheetId": "test-sheet", "tab": "Sheet1", "externalId": "stable-id", "metadataId": 10,
                     "row": ["00:01:00", "00:01:07", "Lula", "=literal text", "Novo"]}

    def get_receipt(self, **kw):
        def read():
            if kw["metadataId"] not in self.receipts:
                raise self.http_error(404)
            return self.receipts[kw["metadataId"]]
        return Request(read)

    def batch(self, **kw):
        def commit():
            append, create = kw["body"]["requests"]
            metadata = create["createDeveloperMetadata"]["developerMetadata"]
            if metadata["metadataId"] in self.receipts:
                raise self.http_error(400)
            self.receipts[metadata["metadataId"]] = metadata
            self.rows.append(append["appendCells"])
            if self.lose_reply:
                self.lose_reply = False
                raise TimeoutError("reply lost after commit")
            return {}
        return Request(commit)

    def test_literal_values_no_extra_columns_and_idempotent_retry(self):
        self.client.send(self.item)
        self.client.send(self.item)
        self.assertEqual(len(self.rows), 1)
        row = self.rows[0]
        self.assertEqual(row["fields"], "userEnteredValue")
        values = row["rows"][0]["values"]
        self.assertEqual(len(values), 5)  # F and later columns are untouched
        self.assertEqual(values[3], {"userEnteredValue": {"stringValue": "=literal text"}})

    def test_response_loss_recovers_without_second_append(self):
        self.lose_reply = True
        with self.assertRaises(DeliveryUnknown):
            self.client.send(self.item)
        self.client.send(self.item)
        self.assertEqual(len(self.rows), 1)

    def test_duplicate_metadata_rejects_entire_retried_batch(self):
        self.client.send(self.item)
        # Simulate a stale preflight lookup followed by a concurrent completed write.
        original = self.client.receipt
        reads = iter([False, True])
        self.client.receipt = lambda item: next(reads)
        self.client.send(self.item)
        self.client.receipt = original
        self.assertEqual(len(self.rows), 1)

    def test_wrong_layout_does_not_write(self):
        self.client.api.values = lambda: SimpleNamespace(get=lambda **kw: Request(lambda: {"values": [["wrong"]]}))
        with self.assertRaises(ValueError):
            self.client.send(self.item)
        self.assertEqual(self.rows, [])

    def test_metadata_collision_does_not_mark_a_different_item_sent(self):
        self.client.send(self.item)
        with self.assertRaises(ValueError):
            self.client.send({**self.item, "externalId": "other-item"})
        self.assertEqual(len(self.rows), 1)


if __name__ == "__main__":
    unittest.main()
