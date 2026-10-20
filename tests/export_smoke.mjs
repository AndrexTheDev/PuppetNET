/**
 * export_smoke.mjs — post-build contract for the Cloudflare Pages static export.
 *
 *   npm run build && npm run test:export
 *
 * The Pages deployment is served from `apps/web/out`, so the built directory is
 * the product. A successful `next build` alone does not prove that every asset
 * the HTML points at was exported, that the ForceAtlas2 worker stayed a classic
 * (non-module) worker, that no server-only output slipped into the export, that
 * every published entity has a complete prerendered profile (canonical, Open
 * Graph, JSON-LD, resolvable links) and that the sitemap only lists files that
 * exist. This gate asserts those properties on the shipped files and exits
 * non-zero on the first violation.
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

/* ------------------------------------------------------------------------- */
/* Entity profiles: static, crawlable and self-contained                     */
/* ------------------------------------------------------------------------- */
const entityDir = path.join(OUT, 'entity');
assert.equal(existsSync(path.join(entityDir, '__empty-publication')), false, 'Reserved empty-export stub was not cleaned by postbuild.');
const publicationFile = path.join(OUT, 'data/graph_latest.json');
const publication = existsSync(publicationFile) ? JSON.parse(readFileSync(publicationFile, 'utf8')) : null;
assert.ok(existsSync(entityDir) || publication?.nodes.length === 0, 'out/entity fehlt: keine vorgerenderten Profilseiten.');
const profiles = (existsSync(entityDir) ? readdirSync(entityDir, { withFileTypes: true }) : [])
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
  .filter((name) => existsSync(path.join(entityDir, name, 'index.html')));
if (publication) {
  const expected = publication.nodes.map(node => node.slug).sort();
  assert.deepEqual([...profiles].sort(), expected, 'Profile und veröffentlichter Graph müssen dieselben Entitäten enthalten.');
} else {
  assert.ok(profiles.length >= 8, `Nur ${profiles.length} Demo-Profilseite(n) exportiert.`);
}
assert.equal(existsSync(path.join(entityDir, 'doc-demo-internal')), false, 'Ein public:false-Dokument wurde exportiert.');

const pngSize = (file) => {
  const buffer = readFileSync(file);
  assert.equal(buffer.subarray(1, 4).toString('ascii'), 'PNG', `${relative(file)} ist kein PNG.`);
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
};
for (const card of ['site', 'person', 'organization', 'location', 'craft', 'document']) {
  const file = path.join(OUT, 'og', `${card}.png`);
  assert.ok(existsSync(file), `OG-Karte out/og/${card}.png fehlt.`);
  assert.deepEqual(pngSize(file), { width: 1200, height: 630 }, `OG-Karte ${card}.png hat falsche Maße.`);
}

const seenEntityIds = new Set();
for (const profile of profiles) {
  const html = readFileSync(path.join(entityDir, profile, 'index.html'), 'utf8');
  const label = `entity/${profile}/index.html`;
  assert.match(html, /<html lang="de"/, `${label} ist nicht deutschsprachig.`);
  assert.match(html, /<h1>[^<]+<\/h1>/, `${label} hat keine H1.`);
  assert.match(html, /<link rel="canonical" href="https:\/\/[^"]+\/entity\/[^"]+\/"\s*\/?>/, `${label} hat keine absolute Canonical-URL.`);
  assert.match(html, /<meta property="og:title" content="[^"]+"/, `${label} hat kein og:title.`);
  assert.match(html, /<meta property="og:image" content="https:\/\/[^"]+\/og\/[a-z]+\.png"/, `${label} hat kein absolutes og:image.`);
  assert.match(html, /<meta property="og:image:width" content="1200"/, `${label} hat keine OG-Bildmaße.`);
  assert.match(html, /<meta name="twitter:card" content="summary_large_image"/, `${label} hat keine Twitter-Card.`);
  assert.match(html, /<meta name="robots" content="index, follow/, `${label} ist nicht indexierbar.`);
  assert.doesNotMatch(html, /(?:localhost|127\.0\.0\.1):\d+/, `${label} enthält eine lokale Server-URL.`);

  const jsonLd = html.match(/<script type="application\/ld\+json">(.*?)<\/script>/s);
  assert.ok(jsonLd, `${label} enthält kein JSON-LD.`);
  const structured = JSON.parse(jsonLd[1]);
  assert.equal(structured['@context'], 'https://schema.org');
  const types = structured['@graph'].map((node) => node['@type']);
  assert.ok(types.includes('BreadcrumbList'), `${label} hat keine Breadcrumb-Daten.`);
  assert.ok(types.some((type) => ['Person', 'Organization', 'Place', 'Vehicle'].includes(type)), `${label} hat keinen Schema.org-Entitätstyp.`);
  const entityNode = structured['@graph'][1];
  assert.match(entityNode['@id'], /#entity$/, `${label} hat keine stabile @id.`);
  assert.equal(seenEntityIds.has(entityNode['@id']), false, `${label} teilt sich eine @id mit einer anderen Seite.`);
  seenEntityIds.add(entityNode['@id']);

  // Internal links must resolve: a dead link in static HTML is a 404 with no server to fix it.
  for (const [, target] of html.matchAll(/href="(\/[^"]*)"/g)) {
    if (target.startsWith('//')) continue;
    const clean = target.split(/[?#]/)[0];
    const candidate = path.join(OUT, clean);
    const resolved = clean.endsWith('/') ? path.join(candidate, 'index.html') : candidate;
    assert.ok(existsSync(resolved), `${label} verlinkt auf fehlende Datei ${target}.`);
  }
}

/* ------------------------------------------------------------------------- */
/* Archived /legacy console: a redirect, never a document                     */
/* ------------------------------------------------------------------------- */
const legacyRedirect = path.join(OUT, 'legacy', 'index.html');
assert.ok(existsSync(legacyRedirect), 'out/legacy/index.html fehlt: der statische Redirect für die archivierte Konsole wurde nicht exportiert.');
const redirectHtml = readFileSync(legacyRedirect, 'utf8');
assert.match(redirectHtml, /<meta http-equiv="refresh" content="0; url=\/dashboard\/"\/?>/,
  'Der Legacy-Pfad braucht einen verzögerungsfreien Meta-Refresh auf /dashboard/ (auch ohne JavaScript).');
assert.match(redirectHtml, /rel="canonical" href="https:\/\/[^"]+\/dashboard\/"/,
  'Der Redirect muss kanonisch auf /dashboard/ zeigen.');
assert.match(redirectHtml, /name="robots" content="noindex/, 'Der Redirect darf nicht indexiert werden.');
assert.match(redirectHtml, /href="\/dashboard\/"/, 'Ein sichtbarer Fallback-Link muss die Weiterleitung erhärten.');
assert.doesNotMatch(redirectHtml, /cytoscape|PuppetNETModals/, 'In /legacy darf kein Konsolen-Code mehr liegen.');
assert.equal(existsSync(path.join(OUT, 'legacy', 'app.js')), false, 'Legacy-Konsolen-Skripte dürfen nicht mehr exportiert werden.');
const redirectsFile = path.join(OUT, '_redirects');
assert.ok(existsSync(redirectsFile), 'out/_redirects fehlt: Cloudflare Pages braucht die 301-Regeln.');
assert.match(readFileSync(redirectsFile, 'utf8'), /^\/legacy\s+\/dashboard\/\s+301$/m, 'Die 301-Regel /legacy → /dashboard/ fehlt.');

/* ------------------------------------------------------------------------- */
/* Sitemap (only when the build had an origin)                               */
/* ------------------------------------------------------------------------- */
const sitemapFile = path.join(OUT, 'sitemap.xml');
if (existsSync(sitemapFile)) {
  const xml = readFileSync(sitemapFile, 'utf8');
  assert.match(xml, /<urlset|<sitemapindex/, 'sitemap.xml ist weder urlset noch sitemapindex.');
  assert.doesNotMatch(xml, /\/legacy/, 'Die Sitemap darf den archivierten /legacy-Pfad nicht führen.');
  for (const [, loc] of xml.matchAll(/<loc>([^<]+)<\/loc>/g)) {
    assert.match(loc, /^https?:\/\//, `Sitemap-Eintrag ${loc} ist nicht absolut.`);
    const pathname = new URL(loc).pathname;
    const candidate = path.join(OUT, decodeURIComponent(pathname));
    const resolved = pathname.endsWith('/') ? path.join(candidate, 'index.html') : candidate;
    assert.ok(existsSync(resolved), `Sitemap verweist auf fehlende Datei ${pathname}.`);
  }
  assert.doesNotMatch(xml, /doc-demo-internal|unlisted-vehicle-holding/, 'Sitemap enthält nicht-öffentliche Entitäten.');
}

const headers = readFileSync(path.join(OUT, '_headers'), 'utf8');
assert.match(headers, /\/_next\/static\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: public, max-age=31536000, immutable/, 'Gehashte Next-Assets werden nicht immutable gecacht.');
assert.match(headers, /\/dashboard\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: no-cache/, 'Dashboard-HTML wird nicht revalidiert.');
assert.match(headers, /\/entity\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: no-cache/, 'Profil-HTML wird nicht revalidiert.');
assert.match(headers, /\/og\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: public, max-age=604800/, 'OG-Karten werden nicht gecacht.');

const totalBytes = files.reduce((sum, file) => sum + statSync(file).size, 0);
assert.ok(totalBytes < 25 * 1024 * 1024, `Static export ist unerwartet groß (${(totalBytes / 1024 / 1024).toFixed(1)} MiB).`);

console.log(`PASS: static export apps/web/out — ${files.length} Dateien, ${(totalBytes / 1024 / 1024).toFixed(2)} MiB`);
console.log(`PASS: HTML assets resolve, ${scripts.length} JS chunks, classic ForceAtlas2 worker ${relative(workerChunks[0])}`);
console.log(`PASS: ${profiles.length} entity profiles with JSON-LD, Open Graph and canonical URLs`);
console.log('PASS: Cloudflare headers cache hashed assets, OG cards and revalidate HTML shells');
