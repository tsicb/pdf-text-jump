
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { reconcilePdfCatalog } from '../scripts/pdf-catalog.mjs';

test('deleted PDFs are excluded and new PDFs are auto-added', () => {
  const items = reconcilePdfCatalog([
    { file: 'removed.pdf', label: '削除済' },
    { file: 'old.pdf', label: 'カスタム名', thumbnailPage: 25, selected: false, renderMode: 'preview' },
  ], ['old.pdf', 'new.pdf']);
  assert.deepEqual(items.map(x => x.file), ['old.pdf', 'new.pdf']);
  assert.equal(items[0].label, 'カスタム名');
  assert.equal(items[0].selected, false);
  assert.equal(items[0].thumbnailPage, 25);
  assert.equal(items[0].renderMode, 'preview');
  assert.equal(items[1].selected, true);
});

test('explicitly disabled PDFs never return via auto-discovery', () => {
  const items = reconcilePdfCatalog([{file: 'hidden.pdf', enabled: false}], ['hidden.pdf', 'visible.pdf']);
  assert.deepEqual(items.map(x=>x.file), ['visible.pdf']);
});

test('NFC and NFD variants use the real filename without duplication', () => {
  const actual = 'レポート.pdf'.normalize('NFD');
  const items = reconcilePdfCatalog([{file: 'レポート.pdf', label: '正式名称'}], [actual, actual.normalize('NFC')]);
  assert.equal(items.length, 1);
  assert.equal(items[0].file, actual);
  assert.equal(items[0].label, '正式名称');
});

test('existing repository inventory matches catalog output', async () => {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const cfg = JSON.parse(await readFile(path.join(root, 'pdfs.json'), 'utf8'));
  const files = (await readdir(path.join(root, cfg.baseDir), {withFileTypes:true}))
    .filter(x=>x.isFile() && x.name.toLowerCase().endsWith('.pdf')).map(x=>x.name);
  const items = reconcilePdfCatalog(cfg.files, files);
  assert.deepEqual(items.map(x=>x.file).sort(), files.sort());
  assert.equal(new Set(items.map(x=>x.file.normalize('NFC'))).size, items.length);
});
