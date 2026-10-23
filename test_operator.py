"""Regression checks for reversible operator changes; no external services."""
import io
import json
import tempfile
import unittest
from types import SimpleNamespace

from server import Handler, Store


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.append("a", "first")
        self.append("b", "second")
        self.append("c", "third")

    def append(self, bid, text, final=True, status="ok"):
        with open(self.store.blocks_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(id=bid, text=text, final=final, status=status,
                                   start="2026-10-23T00:01:00-04:00")) + "\n")

    def test_edit_merge_restore_unmerge_and_reload(self):
        self.store.set_edits({"a": "corrected first"})
        self.store.set_edits({"a": "corrected first"})
        self.store.set_label("a", "1")
        gid = self.store.merge_down("a")
        self.assertEqual(self.store.state()["blocks"][0]["text"], "corrected first second")
        self.assertEqual(self.store.labels[gid], "1")
        self.store.set_edits({gid: "combined correction"})
        self.store = Store(self.temp.name)
        with self.assertRaises(ValueError):
            self.store.unmerge(gid)
        self.store.set_edits({gid: None})
        self.assertEqual(self.store.state()["blocks"][0]["text"], "first second")
        self.assertTrue(self.store.state()["blocks"][0]["merged"])
        self.store.unmerge(gid, confirmed=True)
        self.assertEqual([b["text"] for b in self.store.state()["blocks"]],
                         ["corrected first", "second", "third"])

    def test_nested_merges_preserve_prior_group_edits(self):
        inner = self.store.merge_down("a")
        self.store.set_edits({inner: "corrected group"})
        outer = self.store.merge_down(inner)
        self.assertEqual(len(self.store.state()["blocks"]), 1)
        self.store.unmerge(outer)
        self.assertEqual(self.store.state()["blocks"][0]["text"], "corrected group")
        self.store.unmerge(inner, confirmed=True)
        self.assertEqual(len(self.store.state()["blocks"]), 3)

    def test_conflicts_live_blocks_and_gaps(self):
        self.store.set_label("a", "1")
        self.store.set_label("b", "2")
        with self.assertRaises(ValueError):
            self.store.merge_down("a")
        self.store.set_label("b", "1")
        self.append("b", "live", final=False)
        with self.assertRaises(ValueError):
            self.store.merge_down("a")
        with self.assertRaises(ValueError):
            self.store.set_edits({"b": "edit"})
        self.store.set_label("b", "2")  # labeling live blocks remains allowed
        self.append("b", "gap", status="failed")
        with self.assertRaises(ValueError):
            self.store.merge_down("a")

    def test_live_send_rejected_before_enqueue(self):
        self.append("a", "live", final=False)
        self.store.set_label("a", "1")
        body = json.dumps({"blockIds": ["a"]}).encode()
        responses = []
        handler = SimpleNamespace(store=self.store, rfile=io.BytesIO(body),
                                  outbox=SimpleNamespace(cfg=SimpleNamespace(missing=lambda: [])),
                                  _json=lambda code, data: responses.append((code, data)))
        Handler._send_speech(handler, len(body))
        self.assertEqual(responses[0][0], 400)
        self.assertIn("finalizar", responses[0][1]["error"])

    def test_settings_persist_and_validate_timezone(self):
        settings = {"debate_start": "2026-10-22T23:30:00-04:00", "show_minutage": True}
        self.store.set_settings(settings)
        self.assertEqual(Store(self.temp.name).state()["settings"], settings)
        with self.assertRaises(ValueError):
            self.store.set_settings({**settings, "debate_start": "2026-10-22T23:30:00"})


if __name__ == "__main__":
    unittest.main()
