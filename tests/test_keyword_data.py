#!/usr/bin/env python3
"""Check that regional charts retain all 50 bottom-axis keyword labels."""
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/build-keyword-data.py"
spec = importlib.util.spec_from_file_location("keyword_builder", SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class MockPage:
    rect = SimpleNamespace(width=1000.0, height=1000.0)

    def __init__(self, labels):
        # PDF x-axis labels are extracted as narrow, vertically aligned words.
        self.words = [
            (55.0 + i * 17.2, 770.0, 57.0 + i * 17.2, 783.0,
             word, 0, i, i)
            for i, word in enumerate(labels)
        ]

    def get_text(self, mode):
        assert mode == "words"
        return self.words


class AreaKeywordDataTests(unittest.TestCase):
    def test_right_edge_50th_axis_label_is_not_clipped(self):
        labels = ["A" for _ in range(50)]
        labels[0] = "X"
        labels[-1] = "Z"
        result = builder.graph_labels(MockPage(labels))
        self.assertEqual(len(result), 50)
        self.assertEqual(result[0], "X")
        self.assertEqual(result[-1], "Z")

    def test_missing_axis_word_is_not_silently_invented(self):
        result = builder.graph_labels(MockPage(["A"] * 49))
        self.assertEqual(len(result), 49)


if __name__ == "__main__":
    unittest.main()
