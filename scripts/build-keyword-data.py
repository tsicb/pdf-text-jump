#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageOps

ROOT = Path.cwd()
PDF_BASE_DIR = Path(os.environ.get('PDF_BASE_DIR', 'indeedmarketreports'))
OUTPUT_DIR = Path(os.environ.get('KEYWORD_DATA_DIR', 'keyword-data'))
MANIFEST_PATH = OUTPUT_DIR / 'manifest.json'
DATA_VERSION = 1
EXTRACTOR_VERSION = 2
OCR_SCALE = max(8, min(16, int(os.environ.get('KEYWORD_OCR_SCALE', '12'))))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load_previous() -> dict:
    try:
        return json.loads((ROOT / MANIFEST_PATH).read_text('utf-8'))
    except Exception:
        return {}


def previous_by_file(previous: dict) -> dict[str, dict]:
    out = {}
    if not isinstance(previous, dict):
        return out
    if int(previous.get('extractorVersion', 0) or 0) != EXTRACTOR_VERSION:
        return out
    for item in previous.get('files', []) or []:
        file = str(item.get('file', '')).strip()
        if file:
            out[file] = item
    return out


def clean_keyword(value: str) -> str:
    s = str(value or '').replace('\u00a0', ' ')
    s = re.sub(r'[\t\r\n]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'\s*・\s*', '・', s)
    s = re.sub(r'\s+([、。)])', r'\1', s)
    s = re.sub(r'([(])\s+', r'\1', s)
    return s


def word_tuple(raw):
    x0, y0, x1, y1, text, *rest = raw
    return {
        'x0': float(x0), 'y0': float(y0), 'x1': float(x1), 'y1': float(y1),
        'cx': (float(x0) + float(x1)) / 2,
        'cy': (float(y0) + float(y1)) / 2,
        'text': str(text or ''),
    }


def cluster_values(values: list[float], tolerance: float) -> list[list[float]]:
    groups: list[list[float]] = []
    for v in sorted(values):
        if not groups or abs(v - sum(groups[-1]) / len(groups[-1])) > tolerance:
            groups.append([v])
        else:
            groups[-1].append(v)
    return groups


def join_tokens(tokens: list[dict]) -> str:
    if not tokens:
        return ''
    # Group by baseline first so wrapped words such as rank 18 are joined without a space.
    lines: list[list[dict]] = []
    for token in sorted(tokens, key=lambda t: (t['cy'], t['x0'])):
        if not lines:
            lines.append([token])
            continue
        last_cy = sum(x['cy'] for x in lines[-1]) / len(lines[-1])
        if abs(token['cy'] - last_cy) <= 3.6:
            lines[-1].append(token)
        else:
            lines.append([token])

    parts = []
    for line in lines:
        line = sorted(line, key=lambda t: t['x0'])
        s = ''
        prev = None
        for tok in line:
            txt = tok['text'].strip()
            if not txt:
                continue
            if prev is not None:
                gap = tok['x0'] - prev['x1']
                if gap >= 1.2:
                    s += ' '
            s += txt
            prev = tok
        if s:
            parts.append(s)
    return clean_keyword(''.join(parts))




def extract_visual_text(page: fitz.Page, rect: fitz.Rect, gap_threshold: float = 1.2) -> str:
    raw = page.get_text('rawdict', clip=rect)
    lines = []
    for block in raw.get('blocks', []):
        for line in block.get('lines', []):
            chars = []
            for span in line.get('spans', []):
                for ch in span.get('chars', []):
                    c = str(ch.get('c', ''))
                    if not c or c.isspace():
                        continue
                    x0, y0, x1, y1 = ch.get('bbox', (0, 0, 0, 0))
                    chars.append((float(x0), float(x1), c))
            if not chars:
                continue
            chars.sort(key=lambda x: x[0])
            text = ''
            prev_x1 = None
            for x0, x1, c in chars:
                if prev_x1 is not None and x0 - prev_x1 >= gap_threshold:
                    text += ' '
                text += c
                prev_x1 = x1
            text = clean_keyword(text)
            if text:
                lines.append(text)
    return clean_keyword(''.join(lines))


def repair_token_visual_spaces(page: fitz.Page, token: dict) -> dict:
    original = str(token.get('text', '') or '')
    if len(original) < 2:
        return token
    rect = fitz.Rect(token['x0'] - 0.4, token['y0'] - 0.4, token['x1'] + 0.4, token['y1'] + 0.4)
    visual = extract_visual_text(page, rect)
    if visual and visual.replace(' ', '') == clean_keyword(original).replace(' ', ''):
        copy = dict(token)
        copy['text'] = visual
        return copy
    return token


def detect_page_title(page: fitz.Page, excludes: tuple[str, ...] = ()) -> str:
    candidates = []
    for block in page.get_text('blocks'):
        x0, y0, x1, y1, text, *rest = block
        if y0 > page.rect.height * 0.22:
            continue
        cleaned = clean_keyword(text)
        if not cleaned:
            continue
        compact = cleaned.replace(' ', '')
        if any(token.replace(' ', '') in compact for token in excludes):
            continue
        if compact in {'順位キーワード', '順位', 'キーワード'}:
            continue
        candidates.append((float(y0), -len(cleaned), cleaned))
    candidates.sort()
    for _, __, text in candidates:
        if len(text) >= 6:
            return text
    return candidates[0][2] if candidates else ''

def extract_rank_keyword_table(page: fitz.Page) -> dict | None:
    words = [word_tuple(w) for w in page.get_text('words')]
    header_words = [w for w in words if w['text'] == 'キーワード' and w['y0'] < page.rect.height * 0.3]
    if len(header_words) < 4:
        return None
    header_words = sorted(header_words, key=lambda w: w['cx'])[:5]
    if len(header_words) != 5:
        return None

    centers = [w['cx'] for w in header_words]
    boundaries = [0.0]
    for a, b in zip(centers, centers[1:]):
        boundaries.append((a + b) / 2)
    boundaries.append(float(page.rect.width))

    table_top = max(w['y1'] for w in header_words) + 4
    footer_candidates = [
        w['y0'] for w in words
        if w['y0'] > table_top and (w['text'].startswith('※') or w['text'].startswith('*この文書') or w['text'].startswith('本資料の'))
    ]
    table_bottom = min(footer_candidates) - 3 if footer_candidates else page.rect.height * 0.88

    entries = []
    for col in range(5):
        left, right = boundaries[col], boundaries[col + 1]
        expected_min = col * 10 + 1
        expected_max = expected_min + 9
        in_col = [w for w in words if left <= w['cx'] < right and table_top <= w['cy'] <= table_bottom]
        ranks = []
        for w in in_col:
            if re.fullmatch(r'\d{1,2}', w['text'].strip()):
                n = int(w['text'])
                if expected_min <= n <= expected_max:
                    ranks.append((n, w))
        rank_map = {n: w for n, w in ranks}
        if len(rank_map) < 8:
            return None
        ordered = [(n, rank_map[n]) for n in sorted(rank_map)]
        y_centers = [w['cy'] for _, w in ordered]
        row_bounds = [table_top]
        for a, b in zip(y_centers, y_centers[1:]):
            row_bounds.append((a + b) / 2)
        row_bounds.append(table_bottom)

        for idx, (rank, rank_word) in enumerate(ordered):
            y0, y1 = row_bounds[idx], row_bounds[idx + 1]
            tokens = [
                repair_token_visual_spaces(page, w) for w in in_col
                if y0 <= w['cy'] < y1
                and w is not rank_word
                and w['x0'] >= rank_word['x1'] + 2
                and w['text'].strip() not in {'順位', '順', '位', 'キーワード'}
            ]
            keyword = join_tokens(tokens)
            if keyword:
                entries.append({'rank': rank, 'keyword': keyword})

    entries.sort(key=lambda x: x['rank'])
    # Require a complete Top50 to avoid exposing a copy button on arbitrary tables.
    if len(entries) != 50 or [x['rank'] for x in entries] != list(range(1, 51)):
        return None

    title = detect_page_title(page, excludes=('順位', 'キーワード')) or 'キーワードランキング'
    return {
        'type': 'rank_keyword_table',
        'title': title,
        'quality': 'high',
        'keywords': entries,
    }


def graph_labels(page: fitz.Page) -> list[str]:
    words = [word_tuple(w) for w in page.get_text('words')]
    h = float(page.rect.height)
    # The x-axis labels are vertical, thin word fragments. Exclude the wide footer sentence.
    candidates = [
        w for w in words
        if 45 <= w['cx'] <= page.rect.width * 0.86
        and h * 0.72 <= w['y0'] <= h * 0.91
        and (w['x1'] - w['x0']) <= 18
        and len(w['text'].strip()) <= 3
        and w['text'].strip()
    ]
    if not candidates:
        return []

    clusters: list[list[dict]] = []
    for w in sorted(candidates, key=lambda t: t['cx']):
        if not clusters:
            clusters.append([w])
            continue
        mean_x = sum(x['cx'] for x in clusters[-1]) / len(clusters[-1])
        if abs(w['cx'] - mean_x) <= 5.2:
            clusters[-1].append(w)
        else:
            clusters.append([w])

    labels = []
    for cluster in clusters:
        mean_x = sum(x['cx'] for x in cluster) / len(cluster)
        if mean_x > page.rect.width * 0.84:
            continue
        text = ''.join(w['text'].strip() for w in sorted(cluster, key=lambda t: (t['cy'], t['x0'])))
        text = clean_keyword(text)
        if text and text != '検索数':
            labels.append((mean_x, text))
    labels.sort(key=lambda pair: pair[0])
    return [text for _, text in labels]


def find_top50_headers(page: fitz.Page) -> list[dict]:
    words = [word_tuple(w) for w in page.get_text('words')]
    matches = []
    pat = re.compile(r'検索ワード\s*(\d+)位[～〜~-](\d+)位')
    for w in words:
        m = pat.fullmatch(w['text'].replace(' ', ''))
        if m:
            item = dict(w)
            item['startRank'] = int(m.group(1))
            item['endRank'] = int(m.group(2))
            matches.append(item)
    return sorted(matches, key=lambda w: w['cx'])


def find_top50_row_centers(page: fitz.Page, headers: list[dict]) -> list[float]:
    words = [word_tuple(w) for w in page.get_text('words')]
    if len(headers) < 3:
        return []
    top = max(h['y1'] for h in headers) + 2
    # Search until the "検索数比較" heading.
    compare = [w for w in words if '検索数比較' in w['text'] and w['y0'] > top]
    bottom = min((w['y0'] for w in compare), default=page.rect.height * 0.57) - 2

    # Columns 2 and 3 have reliable Unicode in this known template.
    xs = sorted(h['cx'] for h in headers)
    boundaries = [0.0] + [(a + b) / 2 for a, b in zip(xs, xs[1:])] + [float(page.rect.width)]
    ys = []
    for col in (1, 2):
        left, right = boundaries[col], boundaries[col + 1]
        for w in words:
            if left <= w['cx'] < right and top <= w['cy'] <= bottom and w['text'].strip():
                ys.append(w['cy'])
    groups = cluster_values(ys, 3.5)
    centers = [sum(g) / len(g) for g in groups if g]
    # A multi-token row still shares the same y. Keep exactly the first 10 visible rows.
    return centers[:10]


def run_tesseract_cell(page: fitz.Page, rect: fitz.Rect) -> tuple[str, float]:
    if not shutil.which('tesseract'):
        return '', 0.0
    pix = page.get_pixmap(matrix=fitz.Matrix(OCR_SCALE, OCR_SCALE), clip=rect, alpha=False, colorspace=fitz.csRGB)
    image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples).convert('L')
    image = ImageOps.autocontrast(image)
    # Preserve glyph shapes while removing light anti-aliasing and separator lines.
    bw = image.point(lambda p: 0 if p < 215 else 255)
    try:
        import numpy as np
        arr = np.array(bw)
        mask = arr < 128
        row_density = mask.mean(axis=1)
        for y, density in enumerate(row_density):
            if density > 0.34:
                mask[y, :] = False
        ys, xs = np.where(mask)
        if len(xs):
            x0 = max(0, int(xs.min()) - 14)
            x1 = min(arr.shape[1], int(xs.max()) + 15)
            y0 = max(0, int(ys.min()) - 9)
            y1 = min(arr.shape[0], int(ys.max()) + 10)
            clean = Image.fromarray(np.where(mask, 0, 255).astype('uint8')).crop((x0, y0, x1, y1))
        else:
            clean = bw
    except Exception:
        clean = bw

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
        path = Path(tmp.name)
    try:
        clean.save(path)
        proc = subprocess.run(
            ['tesseract', str(path), 'stdout', '-l', 'jpn+eng', '--psm', '7', 'tsv'],
            capture_output=True, text=True, timeout=30,
        )
        tokens = []
        confs = []
        for line in proc.stdout.splitlines()[1:]:
            cols = line.split('\t')
            if len(cols) < 12:
                continue
            text = cols[11].strip()
            if not text:
                continue
            try:
                conf = float(cols[10])
            except Exception:
                conf = 0.0
            tokens.append(text)
            if conf >= 0:
                confs.append(conf)
        text = clean_keyword(''.join(tokens))
        avg_conf = sum(confs) / len(confs) if confs else 0.0
        return text, avg_conf
    except Exception:
        return '', 0.0
    finally:
        try:
            path.unlink()
        except Exception:
            pass


def repair_truncated_label(page: fitz.Page, rank: int, label: str, headers: list[dict], row_centers: list[float]) -> tuple[str, float]:
    if '…' not in label and '...' not in label:
        return label, 100.0
    if len(headers) != 5 or len(row_centers) < 10:
        return label, 0.0

    col = (rank - 1) // 10
    row = (rank - 1) % 10
    centers_x = [h['cx'] for h in headers]
    x_bounds = [max(0.0, headers[0]['x0'] - 20)]
    x_bounds += [(a + b) / 2 for a, b in zip(centers_x, centers_x[1:])]
    x_bounds += [min(float(page.rect.width), headers[-1]['x1'] + 55)]

    ys = row_centers
    top = headers[col]['y1'] + 2 if row == 0 else (ys[row - 1] + ys[row]) / 2
    bottom = (ys[row] + ys[row + 1]) / 2 if row < 9 else ys[row] + (ys[row] - ys[row - 1]) / 2
    rect = fitz.Rect(x_bounds[col] + 3, top, x_bounds[col + 1] - 3, bottom)
    ocr, conf = run_tesseract_cell(page, rect)
    prefix = re.split(r'…|\.\.\.', label, maxsplit=1)[0]
    if not ocr or len(ocr) <= len(prefix):
        return label, conf

    # The graph label provides a trustworthy prefix. Replace the OCR prefix with it;
    # this corrects the occasional low-confidence glyph inside the truncated prefix.
    repaired = prefix + ocr[len(prefix):]
    repaired = clean_keyword(repaired)
    return repaired, conf


def extract_top50_search_ranking(page: fitz.Page) -> dict | None:
    text = page.get_text('text')
    if 'Top50検索ワードランキング' not in text.replace(' ', ''):
        return None
    headers = find_top50_headers(page)
    if len(headers) != 5:
        return None

    labels = graph_labels(page)
    if len(labels) != 50:
        return None

    row_centers = find_top50_row_centers(page, headers)
    entries = []
    quality = 'high'
    notes = []
    for rank, label in enumerate(labels, 1):
        value = label
        conf = 100.0
        if '…' in value or '...' in value:
            value, conf = repair_truncated_label(page, rank, value, headers, row_centers)
            if '…' in value or '...' in value or conf < 35:
                quality = 'review'
                notes.append(f'{rank}位は長いラベルのOCR補完結果を要確認')
        value = clean_keyword(value)
        if not value:
            return None
        entries.append({'rank': rank, 'keyword': value})

    title_line = ''
    for line in text.splitlines():
        cleaned = clean_keyword(line)
        if 'Top50検索ワードランキング' in cleaned.replace(' ', ''):
            title_line = cleaned
            break
    return {
        'type': 'top50_search_ranking',
        'title': title_line or 'Top50検索ワードランキング',
        'quality': quality,
        'notes': notes,
        'keywords': entries,
    }


def extract_page(page: fitz.Page) -> dict | None:
    for extractor in (extract_rank_keyword_table, extract_top50_search_ranking):
        try:
            result = extractor(page)
        except Exception as exc:
            print(f'    extractor warning page {page.number + 1}: {exc}')
            result = None
        if result:
            result['page'] = page.number + 1
            result['count'] = len(result.get('keywords', []))
            return result
    return None


def extract_file(relative: str, digest: str) -> dict:
    pdf_path = ROOT / PDF_BASE_DIR / relative
    doc = fitz.open(pdf_path)
    pages = []
    try:
        for i in range(doc.page_count):
            page = doc.load_page(i)
            result = extract_page(page)
            if result:
                pages.append(result)
                print(f'  KEYWORD {relative} p.{i + 1}: {result["type"]} / {result["count"]}件 / {result["quality"]}')
    finally:
        doc.close()
    return {
        'file': relative,
        'sha256': digest,
        'pages': pages,
    }


def main() -> int:
    pdf_root = ROOT / PDF_BASE_DIR
    if not pdf_root.is_dir():
        raise RuntimeError(f'PDF directory not found: {pdf_root}')
    pdf_files = sorted(
        [p for p in pdf_root.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf'],
        key=lambda p: p.relative_to(pdf_root).as_posix(),
    )
    if not pdf_files:
        raise RuntimeError(f'No PDF files found under {PDF_BASE_DIR}')

    previous = load_previous()
    prev_map = previous_by_file(previous)
    files = []
    reused = 0
    built = 0

    print(f'Keyword data: {len(pdf_files)} PDFs / extractor v{EXTRACTOR_VERSION}')
    for path in pdf_files:
        relative = path.relative_to(pdf_root).as_posix()
        digest = sha256_file(path)
        prev = prev_map.get(relative)
        if prev and prev.get('sha256') == digest:
            files.append(prev)
            reused += 1
            print(f'REUSE {relative} ({len(prev.get("pages", []) or [])} keyword pages)')
            continue
        print(f'SCAN {relative}')
        files.append(extract_file(relative, digest))
        built += 1

    draft = {
        'version': DATA_VERSION,
        'extractorVersion': EXTRACTOR_VERSION,
        'baseDir': PDF_BASE_DIR.as_posix().rstrip('/') + '/',
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'files': files,
    }

    def comparable(value: dict) -> str:
        copy = dict(value or {})
        copy.pop('generatedAt', None)
        return json.dumps(copy, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    if previous and comparable(previous) == comparable(draft):
        draft['generatedAt'] = previous.get('generatedAt', draft['generatedAt'])

    (ROOT / OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    (ROOT / MANIFEST_PATH).write_text(json.dumps(draft, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    keyword_pages = sum(len(f.get('pages', []) or []) for f in files)
    print(f'DONE: {len(files)} PDFs / {keyword_pages} keyword pages / {built} scanned / {reused} reused')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
