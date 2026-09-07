import { createHash } from 'node:crypto';
import { readdir, readFile, writeFile, mkdir, stat, unlink } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs';

const ROOT = process.cwd();
const PDF_BASE_DIR = normalizeBaseDir(process.env.PDF_BASE_DIR || 'indeedmarketreports/');
const OUTPUT_DIR = String(process.env.SEARCH_INDEX_DIR || 'search-index').replace(/[/\\]+$/, '');
const OUTPUT_FILES_DIR = path.join(OUTPUT_DIR, 'files');
const MANIFEST_PATH = path.join(OUTPUT_DIR, 'manifest.json');
const BUILD_CONCURRENCY = Math.max(1, Math.min(4, Number.parseInt(process.env.INDEX_BUILD_CONCURRENCY || '2', 10) || 2));
const INDEX_VERSION = 1;
const NORMALIZATION_ID = 'NFKC-remove-whitespace-lower-ja-v1';

function normalizeBaseDir(value) {
  let result = String(value || '').trim().replaceAll('\\', '/').replace(/^\/+/, '');
  if (result && !result.endsWith('/')) result += '/';
  return result || 'indeedmarketreports/';
}

function normalizeForSearch(value) {
  return String(value ?? '')
    .normalize('NFKC')
    .replace(/[\s\u00A0\u2000-\u200B\u3000]+/g, '')
    .toLocaleLowerCase('ja-JP');
}

function toPosix(value) {
  return String(value).split(path.sep).join('/');
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

async function exists(filePath) {
  try {
    await stat(filePath);
    return true;
  } catch {
    return false;
  }
}

async function walkPdfFiles(directory, prefix = '') {
  const absolute = path.join(directory, prefix);
  const entries = await readdir(absolute, { withFileTypes: true });
  const files = [];

  for (const entry of entries) {
    const relative = path.join(prefix, entry.name);
    if (entry.isDirectory()) {
      files.push(...await walkPdfFiles(directory, relative));
    } else if (entry.isFile() && entry.name.toLowerCase().endsWith('.pdf')) {
      files.push(toPosix(relative));
    }
  }
  return files;
}

async function readPreviousManifest() {
  try {
    return JSON.parse(await readFile(path.join(ROOT, MANIFEST_PATH), 'utf8'));
  } catch {
    return null;
  }
}

function previousByFile(previous) {
  const map = new Map();
  const files = Array.isArray(previous?.files) ? previous.files : [];
  for (const item of files) {
    if (item?.file) map.set(String(item.file).normalize('NFC'), item);
  }
  return map;
}

async function extractPdfText(relativeFile, bytes, digest) {
  const startedAt = performance.now();
  const loadingTask = getDocument({
    data: new Uint8Array(bytes),
    enableXfa: true,
    isEvalSupported: false,
    useSystemFonts: false,
    verbosity: 0,
  });
  const pdf = await loadingTask.promise;
  const pages = [];
  let textChars = 0;

  try {
    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber++) {
      const page = await pdf.getPage(pageNumber);
      const textContent = await page.getTextContent();
      const pageText = textContent.items.map(item => item?.str ?? '').join(' ');
      const normalized = normalizeForSearch(pageText);
      textChars += normalized.length;
      pages.push({ p: pageNumber, t: normalized });
      page.cleanup();

      if (pageNumber === 1 || pageNumber % 25 === 0 || pageNumber === pdf.numPages) {
        process.stdout.write(`\r  ${relativeFile}: ${pageNumber}/${pdf.numPages} pages`);
      }
    }
    process.stdout.write('\n');

    return {
      payload: {
        version: INDEX_VERSION,
        normalization: NORMALIZATION_ID,
        file: relativeFile,
        sha256: digest,
        pageCount: pdf.numPages,
        textChars,
        pages,
      },
      pages: pdf.numPages,
      textChars,
      elapsedMs: Math.round(performance.now() - startedAt),
    };
  } finally {
    await pdf.destroy();
  }
}

async function buildOne(relativeFile, previousMap) {
  const absolutePath = path.join(ROOT, PDF_BASE_DIR, ...relativeFile.split('/'));
  const bytes = await readFile(absolutePath);
  const digest = sha256(bytes);
  const outputRelative = `${OUTPUT_DIR}/files/${digest}.json`;
  const outputAbsolute = path.join(ROOT, outputRelative);
  const previous = previousMap.get(relativeFile.normalize('NFC'));

  if (
    previous?.sha256 === digest &&
    previous?.index === outputRelative &&
    await exists(outputAbsolute)
  ) {
    console.log(`REUSE ${relativeFile} (${previous.pages || '?'} pages)`);
    return {
      file: relativeFile,
      index: outputRelative,
      pages: Number(previous.pages) || 0,
      textChars: Number(previous.textChars) || 0,
      sizeBytes: bytes.byteLength,
      sha256: digest,
      reused: true,
    };
  }

  console.log(`BUILD ${relativeFile} (${(bytes.byteLength / 1024 / 1024).toFixed(1)} MB)`);
  const built = await extractPdfText(relativeFile, bytes, digest);
  await writeFile(outputAbsolute, JSON.stringify(built.payload), 'utf8');
  console.log(`  -> ${built.pages} pages / ${built.textChars.toLocaleString()} chars / ${(built.elapsedMs / 1000).toFixed(1)}s`);

  return {
    file: relativeFile,
    index: outputRelative,
    pages: built.pages,
    textChars: built.textChars,
    sizeBytes: bytes.byteLength,
    sha256: digest,
    reused: false,
  };
}

async function runPool(items, concurrency, handler) {
  const results = new Array(items.length);
  let cursor = 0;

  const worker = async () => {
    while (true) {
      const index = cursor++;
      if (index >= items.length) return;
      results[index] = await handler(items[index], index);
    }
  };

  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, () => worker()));
  return results;
}

function comparableManifest(value) {
  return JSON.stringify({
    version: value?.version,
    normalization: value?.normalization,
    baseDir: value?.baseDir,
    files: value?.files || [],
    errors: value?.errors || [],
  });
}

async function cleanupOrphans(referencedPaths) {
  const absoluteDir = path.join(ROOT, OUTPUT_FILES_DIR);
  const entries = await readdir(absoluteDir, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
    const relative = toPosix(path.join(OUTPUT_FILES_DIR, entry.name));
    if (!referencedPaths.has(relative)) {
      await unlink(path.join(ROOT, relative));
      console.log(`REMOVE orphan ${relative}`);
    }
  }
}

async function main() {
  const pdfDirectory = path.join(ROOT, PDF_BASE_DIR);
  if (!await exists(pdfDirectory)) {
    throw new Error(`PDF directory not found: ${PDF_BASE_DIR}`);
  }

  await mkdir(path.join(ROOT, OUTPUT_FILES_DIR), { recursive: true });
  const previous = await readPreviousManifest();
  const previousMap = previousByFile(previous);
  const pdfFiles = (await walkPdfFiles(pdfDirectory)).sort((a, b) => a.localeCompare(b, 'ja', { numeric: true }));

  if (!pdfFiles.length) {
    throw new Error(`No PDF files found under ${PDF_BASE_DIR}`);
  }

  console.log(`PDF search index: ${pdfFiles.length} files / concurrency ${BUILD_CONCURRENCY}`);
  const errors = [];
  const results = await runPool(pdfFiles, BUILD_CONCURRENCY, async relativeFile => {
    try {
      return await buildOne(relativeFile, previousMap);
    } catch (error) {
      console.error(`ERROR ${relativeFile}:`, error);
      errors.push({ file: relativeFile, message: String(error?.message || error) });
      return null;
    }
  });

  const files = results
    .filter(Boolean)
    .map(({ reused, ...item }) => item)
    .sort((a, b) => a.file.localeCompare(b.file, 'ja', { numeric: true }));
  errors.sort((a, b) => a.file.localeCompare(b.file, 'ja', { numeric: true }));

  const draft = {
    version: INDEX_VERSION,
    normalization: NORMALIZATION_ID,
    baseDir: PDF_BASE_DIR,
    generatedAt: new Date().toISOString(),
    files,
    errors,
  };

  if (previous && comparableManifest(previous) === comparableManifest(draft)) {
    draft.generatedAt = previous.generatedAt || draft.generatedAt;
  }

  await writeFile(path.join(ROOT, MANIFEST_PATH), `${JSON.stringify(draft, null, 2)}\n`, 'utf8');
  await cleanupOrphans(new Set(files.map(item => item.index)));

  const builtCount = results.filter(item => item && !item.reused).length;
  const reusedCount = results.filter(item => item?.reused).length;
  console.log(`DONE: ${files.length} indexed / ${builtCount} built / ${reusedCount} reused / ${errors.length} errors`);
  if (errors.length) {
    console.log('Files with indexing errors will automatically use browser PDF.js fallback search.');
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
