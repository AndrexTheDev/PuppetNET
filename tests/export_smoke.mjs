/**
 * export_smoke.mjs — post-build contract for the Cloudflare Pages static export.
 *
 *   npm run build && npm run test:export
 *
 * The Pages deployment is served from `apps/web/out`, so the built directory is
 * the product. A successful `next build` alone does not prove that every asset
 * the HTML points at was exported, that the ForceAtlas2 worker stayed a classic
 * (non-module) worker, or that no server-only output slipped into the export.
 * This gate asserts those properties on the shipped files and exits non-zero on
 * the first violation.
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

const OUT = path.resolve('apps/web/out');
assert.ok(existsSync(OUT), 'apps/web/out fehlt. Zuerst `npm run build` ausführen.');
for (const file of ['index.html', 'dashboard/index.html', '404.html', '_headers', 'robots.txt']) {
  assert.ok(existsSync(path.join(OUT, file)), `Static export unvollständig: out/${file} fehlt.`);
}
assert.equal(existsSync(path.join(OUT, '_next/server')), false, 'out/_next/server existiert: kein reiner Static Export.');
assert.equal(existsSync(path.join(OUT, 'dashboard/route.js')), false, 'Route-Handler im Export gefunden.');

const walk = directory => readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
  const target = path.join(directory, entry.name);
  return entry.isDirectory() ? walk(target) : [target];
});
const files = walk(OUT);
const relative = file => path.relative(OUT, file).split(path.sep).join('/');

// Every root-relative src/href in the exported shells must resolve inside out/.
for (const shell of ['index.html', 'dashboard/index.html']) {
  const html = readFileSync(path.join(OUT, shell), 'utf8');
  for (const [, asset] of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
    if (!asset.startsWith('/') || asset.startsWith('//')) continue;
    const clean = asset.split(/[?#]/)[0];
    const candidate = path.join(OUT, clean);
    const isDirectory = clean.endsWith('/') || statSync(candidate, { throwIfNoEntry: false })?.isDirectory();
    const resolved = isDirectory ? path.join(candidate, 'index.html') : candidate;
    assert.ok(existsSync(resolved), `${shell} referenziert fehlende Datei ${asset}.`);
    assert.ok(path.resolve(resolved).startsWith(OUT + path.sep), `${shell} referenziert einen Pfad außerhalb von out/: ${asset}.`);
  }
  assert.doesNotMatch(html, /(?:localhost|127\.0\.0\.1):\d+/, `${shell} enthält eine lokale Server-URL.`);
}

const scripts = files.filter(file => file.endsWith('.js'));
const dashboardChunk = scripts.find(file => relative(file).startsWith('_next/static/chunks/app/dashboard/'));
assert.ok(dashboardChunk, 'Dashboard-JS-Chunk fehlt im Export.');
assert.match(readFileSync(dashboardChunk, 'utf8'), /new Worker\(/, 'Dashboard registriert keinen Web Worker.');

// graphology-layout-forceatlas2 is only reachable through the worker bundle.
// The worker must stay an IIFE: `new Worker(url)` uses the classic worker type.
const workerChunks = scripts.filter(file => {
  const source = readFileSync(file, 'utf8');
  return source.includes('onmessage') && source.includes('postMessage') && source.includes('barnesHut');
});
assert.ok(workerChunks.length > 0, 'Kein ForceAtlas2-Worker-Chunk im Export gefunden.');
for (const chunk of workerChunks) {
  const source = readFileSync(chunk, 'utf8');
  assert.doesNotMatch(source.slice(0, 200), /^\s*(?:import|export)\s/, `${relative(chunk)} ist ein ES-Modul, kein klassischer Worker.`);
  assert.doesNotMatch(source, /\bimport\.meta\b/, `${relative(chunk)} nutzt import.meta im klassischen Worker.`);
}

const headers = readFileSync(path.join(OUT, '_headers'), 'utf8');
assert.match(headers, /\/_next\/static\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: public, max-age=31536000, immutable/, 'Gehashte Next-Assets werden nicht immutable gecacht.');
assert.match(headers, /\/dashboard\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: no-cache/, 'Dashboard-HTML wird nicht revalidiert.');

const totalBytes = files.reduce((sum, file) => sum + statSync(file).size, 0);
assert.ok(totalBytes < 25 * 1024 * 1024, `Static export ist unerwartet groß (${(totalBytes / 1024 / 1024).toFixed(1)} MiB).`);

console.log(`PASS: static export apps/web/out — ${files.length} Dateien, ${(totalBytes / 1024 / 1024).toFixed(2)} MiB`);
console.log(`PASS: HTML assets resolve, ${scripts.length} JS chunks, classic ForceAtlas2 worker ${relative(workerChunks[0])}`);
console.log('PASS: Cloudflare headers cache hashed assets and revalidate the dashboard shell');
