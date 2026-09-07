#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

ROOT = Path.cwd()
PDF_BASE_DIR = Path(os.environ.get('PDF_BASE_DIR', 'indeedmarketreports'))
OUTPUT_DIR = Path(os.environ.get('PAGE_PREVIEW_DIR', 'page-previews'))
OUTPUT_FILES_DIR = OUTPUT_DIR / 'files'
MANIFEST_PATH = OUTPUT_DIR / 'manifest.json'
TARGET_WIDTH = max(320, min(1600, int(os.environ.get('PAGE_PREVIEW_WIDTH', '800'))))
QUALITY = max(25, min(90, int(os.environ.get('PAGE_PREVIEW_QUALITY', '50'))))
CONCURRENCY = max(1, min(4, int(os.environ.get('PAGE_PREVIEW_CONCURRENCY', '2'))))
PREVIEW_VERSION = 1
FORMAT = 'webp'


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
    result = {}
    for item in previous.get('files', []) if isinstance(previous, dict) else []:
        file = str(item.get('file', '')).strip()
        if file:
            result[file] = item
    return result


def output_dir_for_digest(digest: str) -> Path:
    return ROOT / OUTPUT_FILES_DIR / digest


def page_name(page_number: int) -> str:
    return f'p{page_number:04d}.{FORMAT}'


def can_reuse(relative_file: str, digest: str, previous: dict | None) -> bool:
    if not previous or previous.get('sha256') != digest:
        return False
    if int(previous.get('width', 0) or 0) != TARGET_WIDTH:
        return False
    if int(previous.get('quality', 0) or 0) != QUALITY:
        return False
    pages = int(previous.get('pages', 0) or 0)
    base = str(previous.get('previewBase', '')).strip()
    if pages <= 0 or not base:
        return False
    directory = ROOT / base
    return directory.is_dir() and (directory / page_name(1)).is_file() and (directory / page_name(pages)).is_file()


def build_one(args: tuple[str, str]) -> dict:
    relative_file, digest = args
    pdf_path = ROOT / PDF_BASE_DIR / Path(relative_file)
    out_dir = output_dir_for_digest(digest)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    doc = fitz.open(pdf_path)
    try:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            rect = page.rect
            scale = max(0.1, TARGET_WIDTH / max(1.0, float(rect.width)))
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
            image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
            image.save(out_dir / page_name(index + 1), 'WEBP', quality=QUALITY, method=4)
            if index == 0 or (index + 1) % 50 == 0 or index + 1 == doc.page_count:
                print(f'  {relative_file}: {index + 1}/{doc.page_count} pages', flush=True)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            'file': relative_file,
            'sha256': digest,
            'pages': doc.page_count,
            'previewBase': (OUTPUT_FILES_DIR / digest).as_posix(),
            'width': TARGET_WIDTH,
            'quality': QUALITY,
            'format': FORMAT,
            'elapsedMs': elapsed_ms,
            'reused': False,
        }
    finally:
        doc.close()


def main() -> int:
    pdf_root = ROOT / PDF_BASE_DIR
    if not pdf_root.is_dir():
        raise RuntimeError(f'PDF directory not found: {pdf_root}')
    (ROOT / OUTPUT_FILES_DIR).mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(
        [p for p in pdf_root.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf'],
        key=lambda p: p.relative_to(pdf_root).as_posix(),
    )
    if not pdf_files:
        raise RuntimeError(f'No PDF files found under {PDF_BASE_DIR}')

    previous = load_previous()
    prev_map = previous_by_file(previous)
    results: list[dict | None] = [None] * len(pdf_files)
    build_jobs: list[tuple[int, str, str]] = []

    print(f'Page previews: {len(pdf_files)} files / width {TARGET_WIDTH}px / quality {QUALITY} / concurrency {CONCURRENCY}')
    for i, path in enumerate(pdf_files):
        relative = path.relative_to(pdf_root).as_posix()
        digest = sha256_file(path)
        prev = prev_map.get(relative)
        if can_reuse(relative, digest, prev):
            print(f'REUSE {relative} ({prev.get("pages", "?")} pages)')
            results[i] = {
                'file': relative,
                'sha256': digest,
                'pages': int(prev.get('pages', 0) or 0),
                'previewBase': str(prev.get('previewBase')),
                'width': TARGET_WIDTH,
                'quality': QUALITY,
                'format': FORMAT,
                'reused': True,
            }
        else:
            print(f'BUILD {relative}')
            build_jobs.append((i, relative, digest))

    errors = []
    if build_jobs:
        with concurrent.futures.ProcessPoolExecutor(max_workers=CONCURRENCY) as executor:
            futures = {
                executor.submit(build_one, (relative, digest)): (i, relative)
                for i, relative, digest in build_jobs
            }
            for future in concurrent.futures.as_completed(futures):
                i, relative = futures[future]
                try:
                    built = future.result()
                    results[i] = built
                    print(f'  -> {relative}: {built["pages"]} pages / {built["elapsedMs"] / 1000:.1f}s')
                except Exception as exc:
                    print(f'ERROR {relative}: {exc}', file=sys.stderr)
                    errors.append({'file': relative, 'message': str(exc)})

    files = []
    referenced = set()
    built_count = 0
    reused_count = 0
    for item in results:
        if not item:
            continue
        if item.pop('reused', False):
            reused_count += 1
        else:
            built_count += 1
        item.pop('elapsedMs', None)
        files.append(item)
        referenced.add(item['sha256'])

    # Remove preview directories for PDFs that no longer exist or old digests.
    output_root = ROOT / OUTPUT_FILES_DIR
    for child in output_root.iterdir():
        if child.is_dir() and child.name not in referenced:
            shutil.rmtree(child)
            print(f'REMOVE orphan {child.relative_to(ROOT).as_posix()}')

    draft = {
        'version': PREVIEW_VERSION,
        'baseDir': PDF_BASE_DIR.as_posix().rstrip('/') + '/',
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'width': TARGET_WIDTH,
        'quality': QUALITY,
        'format': FORMAT,
        'files': sorted(files, key=lambda x: x['file']),
        'errors': sorted(errors, key=lambda x: x['file']),
    }

    # Preserve generatedAt when the material content is unchanged.
    def comparable(value: dict) -> str:
        copy = dict(value or {})
        copy.pop('generatedAt', None)
        return json.dumps(copy, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    if previous and comparable(previous) == comparable(draft):
        draft['generatedAt'] = previous.get('generatedAt', draft['generatedAt'])

    (ROOT / OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    (ROOT / MANIFEST_PATH).write_text(json.dumps(draft, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(f'DONE: {len(files)} preview sets / {built_count} built / {reused_count} reused / {len(errors)} errors')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
