#!/usr/bin/env python3
"""Regression tests for CSV export logic. Run with unittest discovery."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import fitz

MODULE = Path(__file__).resolve().parent.parent / "scripts/export-keyword-csv.py"
spec = importlib.util.spec_from_file_location("keyword_export", MODULE)
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


class KeywordCsvTests(unittest.TestCase):
    def test_job_title(self):
        value, reason = export.extract_meta("job_top50", "会計カテゴリー/ 会計事務_2026年8月")
        self.assertFalse(reason)
        self.assertEqual(value["month"], "202608")
        self.assertEqual(value["category"], "会計")
        self.assertEqual(value["job"], "会計事務")

    def test_area_title(self):
        value, reason = export.extract_meta(
            "area_top50", "2026年8月 Top50検索ワードランキング対象エリア：東京都")
        self.assertFalse(reason)
        self.assertEqual(value["area"], "東京都")

    def test_nationwide_title(self):
        value, reason = export.extract_meta(
            "area_top50", "2026年8月 Top50検索ワードランキング対象エリア：全国")
        self.assertFalse(reason)
        self.assertEqual(value["area"], "全国")

    def test_formula_escape(self):
        self.assertEqual(export.safe_cell("=HYPERLINK(...)"), "'=HYPERLINK(...)")
        self.assertEqual(export.safe_cell("パート"), "パート")

    def test_four_column_100(self):
        doc = fitz.open()
        for start in (1, 101):
            page = doc.new_page(width=900, height=1050)
            for column in range(4):
                for row in range(25):
                    rank = start + column * 25 + row
                    y = 105 + row * 31
                    page.insert_text((20 + column * 225, y), str(rank), fontsize=10)
                    page.insert_text((55 + column * 225, y), "KW" + str(rank), fontsize=10)
            entries, notes = export.parse_trend_page(page, start)
            self.assertEqual(notes, [], notes)
            self.assertTrue(export.valid_ranks(entries, 100, start))
            self.assertEqual(entries[0]["keyword"], "KW" + str(start))
        doc.close()

    def test_revisions_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {"type": "job_top50", "month": "202608", "start": "",
                    "end": "", "category": "会計", "job": "経理", "area": "",
                    "scope": "", "file": "demo.pdf", "pages": [10], "title": "demo",
                    "quality": "high", "method": "rank_keyword_table",
                    "sha256": "aaa", "rank_basis": "click",
                    "keywords": [{"rank": 1, "keyword": "経理"}]}
            self.assertEqual(export.upsert_dataset(root, base), "added")
            self.assertEqual(export.upsert_dataset(root, base), "unchanged")
            revised = dict(base, keywords=[{"rank": 1, "keyword": "事務"}])
            self.assertEqual(export.upsert_dataset(root, revised), "pending_revision")
            active = list(export.all_active(root))
            self.assertEqual(active[0]["keywords"][0]["keyword"], "経理")
            self.assertEqual(len(list((root / "keyword-history/pending").rglob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
