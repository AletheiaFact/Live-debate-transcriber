"""Offline checks for the exact summary shared by Sheets and the preview."""
import unittest

from ai_delivery import build_ai_cell, selected_evidence, safe_url


def record(key, **changes):
    return {"evidence_key": key, "record_id": key, "sheet_name": "Lula", "score": 1,
            "relevance": "DIRECT_MATCH", "context_compatible": True, "source_quality": "RELIABLE",
            "spreadsheet_row_url": "https://example.org/row/" + key,
            "source_urls": ["https://example.org/source/" + key], **changes}


class SummaryTests(unittest.TestCase):
    def test_compatible_matches_exclude_incompatible_background_and_uncertain(self):
        evidence = [record("wrong", context_compatible=False, score=100),
                    record("background", relevance="BACKGROUND_ONLY"),
                    record("uncertain", source_quality="UNCERTAIN"),
                    record("partial", relevance="PARTIAL_MATCH", score=99),
                    record("direct1", score=2), record("direct2", score=3), record("direct3", score=1)]
        chosen = selected_evidence({"evidence": evidence})
        self.assertEqual([r["record_id"] for r in chosen], ["direct2", "direct1", "direct3"])
        chosen = selected_evidence({"evidence": evidence[:5]})
        self.assertEqual([r["record_id"] for r in chosen], ["direct1", "partial"])

    def test_only_falls_back_to_incompatible_when_no_compatible_exists(self):
        evidence = [record("wrong", context_compatible=False), record("unassessed", context_compatible=None)]
        cell = build_ai_cell([{"claims": [{"quote": "A frase", "explanation": "Resposta", "evidence": evidence}]}])
        self.assertIn("CONTEXTO INCOMPATÍVEL", cell["userEnteredValue"]["stringValue"])
        self.assertNotIn("unassessed", cell["userEnteredValue"]["stringValue"])
        self.assertIn("Sem veredicto", cell["userEnteredValue"]["stringValue"])

    def test_unicode_bold_links_deduplication_and_literal_text(self):
        e = record("record", primary_source_urls=["https://example.org/source/record"])
        e["source_urls"] += ["javascript:alert(1)", "https://example.org/extra", "https://example.org/excess"]
        cell = build_ai_cell([{"claims": [{"quote": "=Uma frase 😀", "suggested_verdict": "FALSO",
                                          "explanation": "Resposta <script> literal", "evidence": [e, e]}]}])
        text = cell["userEnteredValue"]["stringValue"]
        self.assertIn("=Uma frase 😀", text)
        self.assertIn("Resposta <script> literal", text)
        self.assertEqual(text.count("Abrir registro original"), 1)
        links = []
        encoded = text.encode("utf-16-le")
        for i, run in enumerate(cell["textFormatRuns"]):
            end = cell["textFormatRuns"][i + 1]["startIndex"] if i + 1 < len(cell["textFormatRuns"]) else len(encoded) // 2
            excerpt = encoded[run["startIndex"] * 2:end * 2].decode("utf-16-le")
            if run["format"].get("link"):
                links.append((excerpt, run["format"]["link"]["uri"]))
            if excerpt == "FALSO\n":
                self.assertTrue(run["format"]["bold"])
        self.assertEqual([x[0] for x in links], ["Abrir registro original", "Fonte documental 1", "Fonte documental 2"])
        self.assertTrue(all(url.startswith("https://") for _, url in links))

    def test_empty_results_and_large_cells_are_explicit(self):
        self.assertIn("Nenhuma afirmação", build_ai_cell([{"claims": []}])["userEnteredValue"]["stringValue"])
        cell = build_ai_cell([{"claims": [{"quote": "😀" * 30000, "explanation": "x"}]}])
        self.assertLess(len(cell["userEnteredValue"]["stringValue"].encode("utf-16-le")) // 2, 50000)
        self.assertIn("Resumo truncado", cell["userEnteredValue"]["stringValue"])
        for bad in ("javascript:alert(1)", "data:text/html,test", "https:///missing", "https://example.org/\n", "http://["):
            self.assertFalse(safe_url(bad))


if __name__ == "__main__":
    unittest.main()
