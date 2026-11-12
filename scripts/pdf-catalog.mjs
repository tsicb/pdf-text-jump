
/** The actual PDF inventory is authoritative; pdfs.json only supplies display settings. */
function canonicalFileKey(value) {
  return String(value || '').replaceAll('\\', '/').replace(/^\.\//, '').normalize('NFC');
}

export function reconcilePdfCatalog(configuredEntries, availableFileNames) {
  const available = new Map();
  for (const raw of availableFileNames || []) {
    const file = String(raw || '').trim().replaceAll('\\', '/');
    if (!file || !file.toLowerCase().endsWith('.pdf')) continue;
    const key = canonicalFileKey(file);
    if (!available.has(key)) available.set(key, file);
  }
  const catalog = [];
  const registered = new Set();
  for (const item of configuredEntries || []) {
    if (!item?.file) continue;
    const key = canonicalFileKey(item.file);
    if (registered.has(key)) continue;
    registered.add(key);
    const actualFile = available.get(key);
    if (!actualFile || item.enabled === false) continue;
    catalog.push({ ...item, file: actualFile });
  }
  for (const [key, file] of available) {
    if (registered.has(key)) continue;
    catalog.push({
      file, label: file.replace(/\.pdf$/i, ''), description: '',
      thumbnailPage: 1, selected: true, renderMode: 'auto',
    });
  }
  return catalog;
}
