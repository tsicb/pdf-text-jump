#!/usr/bin/env python3
"""Build private, accumulated CSV history from existing Indeed keyword JSON.

The public keyword-data/manifest.json and PDF viewer are never modified here.
Active datasets are immutable unless manually replaced. Changed datasets go
to pending/, while exports continue to use the approved active version.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from pathlib import Path

import fitz

BASE = Path(__file__).resolve().parent.parent
EXPORT_FILES = {
    "job_top50": "keyword_rank_job.csv",
    "area_top50": "keyword_rank_area.csv",
    "trend_top200": "keyword_rank_trend.csv",
}
COLUMNS = [
    "ランキング種別", "対象年月", "集計開始年月", "集計終了年月",
    "カテゴリー", "対象職種", "対象エリア", "順位", "キーワード",
    "順位基準", "元PDFファイル名", "PDFページ", "PDF内タイトル",
    "抽出品質", "抽出方式", "元PDF_SHA256", "データセットID",
]
STATUS_COLUMNS = ["元PDFファイル名", "PDFページ", "ランキング種別",
                  "対象", "状態", "件数", "理由", "データセットID"]


def compact(value):
    return re.sub(r"\s+", "", str(value or "")).replace("：", ":")


def year_month(y, m):
    y, m = int(y), int(m)
    return f"{y:04d}{m:02d}" if 2000 <= y <= 2100 and 1 <= m <= 12 else ""


def safe_cell(value):
    """Prevent formula execution in spreadsheet software while keeping JSON raw."""
    s = str(value if value is not None else "")
    if re.match(r"^\s*[=+\-@\t\r\n]", s):
        return "'" + s
    return s


def clean_keyword(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def extract_meta(kind, title):
    title = str(title or "")
    if kind == "job_top50":
        m = re.search(r"([^\s/]+?)カテゴリー\s*/\s*([^_\n]+?)\s*_\s*(\d{4})年\s*(\d{1,2})月", title)
        if not m:
            return None, "職種名／カテゴリー／対象年月を解析できません"
        ym = year_month(m.group(3), m.group(4))
        category, job = m.group(1).strip(), m.group(2).strip()
        if not (ym and category and job):
            return None, "職種の対象情報が不完全です"
        return {"month": ym, "category": category, "job": job, "area": "",
                "start": "", "end": "", "scope": ""}, ""
    if kind == "area_top50":
        month = re.search(r"(\d{4})年\s*(\d{1,2})月", title)
        area = re.search(r"対象エリア\s*[:：]\s*(.+?)\s*$", title)
        if not month or not area:
            return None, "都道府県／対象年月を解析できません"
        ym = year_month(month.group(1), month.group(2))
        region = area.group(1).strip()
        if not ym or not re.fullmatch(r"(?:全国|北海道|東京都|京都府|大阪府|.{2,3}県)", region):
            return None, "地域表記を確認できません"
        return {"month": ym, "category": "", "job": "", "area": region,
                "start": "", "end": "", "scope": ""}, ""
    return None, "未対応のランキング種別"


def valid_ranks(keywords, size, start=1):
    if len(keywords) != size:
        return False
    try:
        ranks = [int(x["rank"]) for x in keywords]
        return sorted(ranks) == list(range(start, start + size)) and all(
            clean_keyword(x.get("keyword")) for x in keywords)
    except (KeyError, ValueError, TypeError):
        return False


def dataset_key(row):
    parts = [row["type"], row["month"], row["start"], row["end"],
             row["category"], row["job"], row["area"], row["scope"]]
    return "|".join(parts)


def dataset_path(row):
    fingerprint = hashlib.sha256(dataset_key(row).encode("utf-8")).hexdigest()[:24]
    return f'{row["type"]}/{fingerprint}'


def normalized_keyword_rows(keywords):
    return [{"rank": int(x["rank"]), "keyword": clean_keyword(x["keyword"])}
            for x in sorted(keywords, key=lambda x: int(x["rank"]))]


def upsert_dataset(root: Path, row: dict):
    key = dataset_path(row)
    active = root / "keyword-history" / "active" / (key + ".json")
    pending_dir = root / "keyword-history" / "pending" / key
    active.parent.mkdir(parents=True, exist_ok=True)
    snapshot = json.dumps(row, ensure_ascii=False, indent=2) + "\n"
    if not active.exists():
        active.write_text(snapshot, encoding="utf-8")
        return "added"
    current = json.loads(active.read_text(encoding="utf-8"))
    if current["type"] != row["type"] or dataset_key(current) != dataset_key(row):
        raise ValueError(f"データセットID衝突: {key}")
    if normalized_keyword_rows(current["keywords"]) == normalized_keyword_rows(row["keywords"]):
        return "unchanged"
    pending_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        json.dumps(normalized_keyword_rows(row["keywords"]), ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    target = pending_dir / (digest + ".json")
    if not target.exists():
        target.write_text(snapshot, encoding="utf-8")
    return "pending_revision"


def pdf_page_numbers(pages):
    return ",".join(str(p) for p in sorted(set(pages)))


def collect_keyword_manifest(manifest):
    rows, status = [], []
    for f in manifest.get("files", []):
        file = str(f.get("file") or "")
        sha = str(f.get("sha256") or "")
        for pg in f.get("pages", []):
            extractor = str(pg.get("type") or "")
            kind = {"rank_keyword_table": "job_top50",
                    "top50_search_ranking": "area_top50"}.get(extractor)
            if not kind:
                continue  # never treat other PDF tables as a ranking
            number = pg.get("page")
            title = str(pg.get("title") or "")
            keywords = pg.get("keywords") or []
            meta, problem = extract_meta(kind, title)
            good_ranks = valid_ranks(keywords, 50)
            quality = str(pg.get("quality") or "review")
            reason = problem or ("" if good_ranks else "順位が1～50で連続していません")
            if quality != "high":
                reason = (reason + " / " if reason else "") + "抽出品質がhighではありません"
            target = (meta["job"] or meta["area"]) if meta else ""
            status_item = {"元PDFファイル名": file, "PDFページ": number,
                           "ランキング種別": kind, "対象": target,
                           "状態": "high" if not reason else "review",
                           "件数": len(keywords), "理由": reason, "データセットID": ""}
            if not reason:
                row = dict(meta, type=kind, keywords=normalized_keyword_rows(keywords),
                           file=file, pages=[int(number)], title=title, quality="high",
                           method=extractor, sha256=sha, rank_basis=("click" if kind == "job_top50" else "search"))
                status_item["データセットID"] = dataset_key(row)
                rows.append(row)
            status.append(status_item)
    return rows, status


def word_tokens(page):
    out = []
    for x0, y0, x1, y1, label, *_ in page.get_text("words"):
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "cy": (y0 + y1) / 2, "text": label})
    return out


def parse_trend_page(page, first_rank):
    """Read four 25-row blocks using their actual rank anchors, not page quarters."""
    tokens = word_tokens(page)
    height = page.rect.height
    entries, diagnostics = [], []
    anchors_by_column = []
    # PDF layouts have asymmetric margins: the four groups do not span equal
    # quarters. Search all x positions for the expected 25 ranks per column.
    candidates = [x for x in tokens if height * .06 < x["cy"] < height * .88]
    for col in range(4):
        low = first_rank + col * 25
        high = low + 24
        anchors = {}
        for x in candidates:
            m = re.fullmatch(r"(\d{1,3})(?:位)?", x["text"])
            if m and low <= int(m.group(1)) <= high:
                anchors.setdefault(int(m.group(1)), []).append(x)
        if len(anchors) != 25 or any(len(v) != 1 for v in anchors.values()):
            diagnostics.append(f"{low}～{high}位の順位アンカーが揃いません: {len(anchors)}/25")
            anchors_by_column.append(None)
        else:
            anchors_by_column.append([(i, anchors[i][0]) for i in range(low, high + 1)])

    for col, ordered in enumerate(anchors_by_column):
        if not ordered:
            continue
        centers = [x["cy"] for _, x in ordered]
        top = centers[0] - (centers[1] - centers[0]) / 2
        bottom = centers[-1] + (centers[-1] - centers[-2]) / 2
        # Take the x coordinate of the next rank column as the cutoff to
        # prevent adjacent column keywords from leaking into a row.
        next_group = anchors_by_column[col + 1] if col < 3 else None
        right = min(x["x0"] for _, x in next_group) - 2 if next_group else page.rect.width
        for idx, (rank, anchor) in enumerate(ordered):
            y0 = top if idx == 0 else (centers[idx - 1] + centers[idx]) / 2
            y1 = bottom if idx == 24 else (centers[idx] + centers[idx + 1]) / 2
            words = [t for t in candidates if y0 <= t["cy"] < y1
                     and t is not anchor and t["x0"] >= anchor["x1"] + .5
                     and t["x0"] < right]
            words.sort(key=lambda t: (round(t["cy"] / 3), t["x0"]))
            keyword = clean_keyword("".join(t["text"] for t in words))
            if not keyword:
                diagnostics.append(f"{rank}位のキーワードが空欄")
            else:
                entries.append({"rank": rank, "keyword": keyword})
    if not valid_ranks(entries, 100, first_rank):
        diagnostics.append(f"{first_rank}～{first_rank+99}位: {len(entries)}/100件")
    return entries, diagnostics



def trend_period(doc):
    for p in range(min(doc.page_count, 5)):
        txt = compact(doc[p].get_text("text"))
        m = re.search(r"期間:?\s*(\d{4})年(\d{1,2})月[~～〜－\-]+(\d{4})年(\d{1,2})月", txt)
        if m:
            return year_month(m.group(1), m.group(2)), year_month(m.group(3), m.group(4))
    return "", ""


def collect_trend(pdf_root: Path, manifest):
    rows, statuses = [], []
    for f in manifest.get("files", []):
        file = str(f.get("file") or "")
        if "求職者検索トレンド" not in file.replace("゙", "").replace("゚", "") and "求職者検索トレンド" not in __import__("unicodedata").normalize("NFKC", file):
            continue
        pdf_path = pdf_root / file
        if not pdf_path.is_file():
            statuses.append({"元PDFファイル名": file, "PDFページ": "",
                             "ランキング種別": "trend_top200", "対象": "",
                             "状態": "error", "件数": 0, "理由": "PDFが見つかりません",
                             "データセットID": ""})
            continue
        doc = fitz.open(pdf_path)
        try:
            begin, end = trend_period(doc)
            by_half = {}
            page_numbers = {}
            notes = []
            for i in range(min(8, doc.page_count)):
                page = doc[i]
                heading = compact(page.get_text("text"))
                marker = re.search(r"検索ワードリスト[\(（](1|101)位[~～〜\-](100|200)位[\)）]", heading)
                if not marker:
                    continue
                first = int(marker.group(1))
                if first in by_half:
                    notes.append(f"{first}位からのランキングが重複しています")
                    continue
                entries, problems = parse_trend_page(page, first)
                by_half[first] = entries
                page_numbers[first] = i + 1
                notes.extend(f"PDF {i+1}ページ: {problem}" for problem in problems)
            combined = by_half.get(1, []) + by_half.get(101, [])
            if not begin or not end:
                notes.append("集計対象期間を解析できません")
            if not valid_ranks(combined, 200):
                notes.append(f"Top200の取得件数が不足・不整合です ({len(combined)}/200)")
            status = {"元PDFファイル名": file,
                      "PDFページ": pdf_page_numbers(list(page_numbers.values())),
                      "ランキング種別": "trend_top200", "対象": f"{begin}～{end}",
                      "状態": "high" if not notes else "review",
                      "件数": len(combined), "理由": " / ".join(notes), "データセットID": ""}
            if not notes:
                row = {"type": "trend_top200", "month": "", "start": begin, "end": end,
                       "category": "", "job": "", "area": "", "scope": "unspecified",
                       "keywords": normalized_keyword_rows(combined), "file": file,
                       "pages": [page_numbers[1], page_numbers[101]],
                       "title": "検索ワードリスト（1位～200位）", "quality": "high",
                       "method": "trend_rank_four_columns", "sha256": str(f.get("sha256") or ""),
                       "rank_basis": "search"}
                rows.append(row)
                status["データセットID"] = dataset_key(row)
            statuses.append(status)
        finally:
            doc.close()
    return rows, statuses


def add_missing_area_page_status(pdf_root, manifest, status):
    """Record detected but not extracted Top50 pages without inventing rows."""
    existing = {(s["元PDFファイル名"], int(s["PDFページ"])) for s in status
                if s["ランキング種別"] == "area_top50" and str(s["PDFページ"]).isdigit()}
    for f in manifest.get("files", []):
        name = __import__("unicodedata").normalize("NFKC", str(f.get("file") or ""))
        if "求職者検索ワードランキング" not in name:
            continue
        filename = str(f.get("file"))
        path = pdf_root / filename
        if not path.is_file():
            continue
        with fitz.open(path) as doc:
            for i, page in enumerate(doc):
                if (filename, i + 1) in existing:
                    continue
                txt = compact(page.get_text("text"))
                if "Top50検索ワードランキング" in txt and "対象エリア" in txt:
                    m = re.search(r"対象エリア:([^\s検索]+)", txt)
                    area = m.group(1) if m else ""
                    # A blank template page in the current report has no
                    # region label or chart. It is not an additional prefecture.
                    blank_template = not area and "検索ワード1位" not in txt
                    status.append({"元PDFファイル名": filename, "PDFページ": i + 1,
                                   "ランキング種別": "area_top50",
                                   "対象": area,
                                   "状態": "skipped_blank" if blank_template else "not_extracted",
                                   "件数": 0,
                                   "理由": ("対象エリア・ランキング情報のない空テンプレート"
                                          if blank_template else
                                          "ランキングを50件確定できません"),
                                   "データセットID": ""})


def csv_write(path, fieldnames, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\r\n", extrasaction="ignore")
        writer.writeheader()
        for item in records:
            writer.writerow({col: safe_cell(item.get(col, "")) for col in fieldnames})


def all_active(private_root):
    root = private_root / "keyword-history" / "active"
    for filename in sorted(root.rglob("*.json")) if root.exists() else []:
        yield json.loads(filename.read_text(encoding="utf-8"))


def generate_exports(private_root, status):
    exports = private_root / "exports"
    source = list(all_active(private_root))
    for kind, filename in EXPORT_FILES.items():
        records = []
        for ds in source:
            if ds["type"] != kind:
                continue
            for kw in ds["keywords"]:
                records.append({
                    "ランキング種別": kind, "対象年月": ds["month"],
                    "集計開始年月": ds["start"], "集計終了年月": ds["end"],
                    "カテゴリー": ds["category"], "対象職種": ds["job"],
                    "対象エリア": ds["area"], "順位": kw["rank"],
                    "キーワード": kw["keyword"], "順位基準": ds["rank_basis"],
                    "元PDFファイル名": ds["file"], "PDFページ": pdf_page_numbers(ds["pages"]),
                    "PDF内タイトル": ds["title"], "抽出品質": ds["quality"],
                    "抽出方式": ds["method"], "元PDF_SHA256": ds["sha256"],
                    "データセットID": dataset_key(ds),
                })
        records.sort(key=lambda x: (x["対象年月"], x["集計開始年月"], x["カテゴリー"],
                                    x["対象職種"], x["対象エリア"], int(x["順位"])))
        csv_write(exports / filename, COLUMNS, records)
        print(f"EXPORT {filename}: {len(records)} rows")
    status.sort(key=lambda s: (s["元PDFファイル名"], str(s["PDFページ"]), s["ランキング種別"]))
    csv_write(exports / "keyword_extract_status.csv", STATUS_COLUMNS, status)


def run(manifest_path: Path, pdf_root: Path, private_root: Path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    private_root.mkdir(parents=True, exist_ok=True)
    datasets, status = collect_keyword_manifest(manifest)
    extra, extra_status = collect_trend(pdf_root, manifest)
    datasets += extra
    status += extra_status
    add_missing_area_page_status(pdf_root, manifest, status)
    status_by_key = {s["データセットID"]: s for s in status if s["データセットID"]}
    counts = {"added": 0, "unchanged": 0, "pending_revision": 0}
    for ds in datasets:
        action = upsert_dataset(private_root, ds)
        counts[action] += 1
        s = status_by_key.get(dataset_key(ds))
        if s and action == "pending_revision":
            s["状態"] = "pending_revision"
            s["理由"] = "同じ期間・対象の既存データと異なります。自動上書きしません"
    generate_exports(private_root, status)
    print(f"ARCHIVE: {counts}, status rows: {len(status)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=BASE / "keyword-data/manifest.json")
    parser.add_argument("--pdf-dir", type=Path, default=BASE / "indeedmarketreports")
    parser.add_argument("--output-repo", type=Path, required=True)
    args = parser.parse_args()
    run(args.manifest, args.pdf_dir, args.output_repo)


if __name__ == "__main__":
    main()
