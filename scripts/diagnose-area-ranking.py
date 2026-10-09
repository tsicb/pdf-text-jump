#!/usr/bin/env python3
"""Inspect why regional Top50 charts fail current keyword extraction."""
import importlib.util
from pathlib import Path
import fitz
import unicodedata

source = Path("scripts/build-keyword-data.py")
spec = importlib.util.spec_from_file_location("build_keywords", source)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

matches = [p for p in Path("indeedmarketreports").rglob("*") if p.is_file()
           and "求職者検索ワードランキング" in unicodedata.normalize("NFKC", p.name)]
assert len(matches) == 1, matches
with fitz.open(matches[0]) as doc:
    print("PDF", matches[0], "pages", len(doc))
    for i in range(1, min(doc.page_count, 51)):
        page = doc[i]
        title = page.get_text("text")
        if "Top50検索ワードランキング" not in title.replace(" ", ""):
            continue
        ls = mod.graph_labels(page)
        hs = mod.find_top50_headers(page)
        ys = mod.find_top50_row_centers(page, hs)
        part = title.replace("\n", "").replace(" ", "")
        place = part.split("対象エリア：",1)[-1].split("検索ランキング",1)[0] if "対象エリア：" in part else "UNKNOWN"
        last = [x[:20] for x in ls[-3:]]
        print(f"AREA_DIAG p={i+1} area={place[:12]} labels={len(ls)} headers={len(hs)} rows={len(ys)} last={last}")
        if i+1 in [2, 4, 6, 13, 14, 29, 32, 50]:
            print("  HEADER_LOC", [(round(h["x0"],1), round(h["y0"],1), h["text"]) for h in hs])
            print("  ROWS", [round(y,1) for y in ys])
            print("  LABELS", [x[:30] for x in ls[:8]], "...", [x[:30] for x in ls[-8:]])
