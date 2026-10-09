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

def variant(page, max_width, max_len, tolerance, upper_fraction):
    words = [mod.word_tuple(w) for w in page.get_text("words")]
    h = page.rect.height
    raw = [w for w in words if 45 <= w["cx"] <= page.rect.width * upper_fraction
           and h * .72 <= w["y0"] <= h * .91
           and (w["x1"] - w["x0"]) <= max_width
           and len(w["text"].strip()) <= max_len
           and w["text"].strip()]
    groups = []
    for w in sorted(raw,key=lambda x:x["cx"]):
        if not groups or abs(w["cx"] - sum(x["cx"] for x in groups[-1]) / len(groups[-1])) > tolerance:
            groups.append([w])
        else:
            groups[-1].append(w)
    accepted = []
    for cluster in groups:
        mean_x = sum(x["cx"] for x in cluster) / len(cluster)
        if mean_x > page.rect.width * (upper_fraction-.02):
            continue
        text = "".join(x["text"].strip() for x in sorted(cluster, key=lambda t:(t["cy"], t["x0"])))
        text = mod.clean_keyword(text)
        if text and text != "検索数":
            accepted.append((mean_x,text))
    return accepted


if __name__ == "__main__":
    with fitz.open(matches[0]) as doc:
        for page_num in [2,4,6,13,14,24,29,32,45,50]:
            page = doc[page_num-1]
            counts = {}
            for width, length, tol, frac in [
                (18,3,5.2,.86),(30,3,5.2,.86),(50,3,5.2,.86),
                (18,5,5.2,.86),(30,6,5.2,.86),(50,10,5.2,.86),
                (18,3,4.0,.86),(18,3,6.5,.86),(25,6,4.0,.86),
                (25,6,5.2,.95),(50,10,5.2,.95)
            ]:
                key=f"{width}/{length}/{tol}/{frac}"
                vals=variant(page,width,length,tol,frac)
                counts[key]=len(vals)
            print("AREA_VARIANTS",page_num,counts)
            vals=variant(page,30,6,5.2,.95)
            dx=[round(vals[i+1][0]-vals[i][0],1) for i in range(len(vals)-1)]
            gaplist=sorted([(v,i,vals[i][1][:10],vals[i+1][1][:10]) for i,v in enumerate(dx)],reverse=True)[:5]
            print("AREA_GAPS",page_num,gaplist)
