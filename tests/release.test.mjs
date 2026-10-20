import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const read = file => readFileSync(file, 'utf8');
test('SEO generator matches the public entity routes, with absolute deployment URLs', () => {
  const result = spawnSync(process.execPath, ['scripts/generate-seo.js'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const manifest = JSON.parse(read('apps/web/public/data/seo-metadata.json'));
  const sitemap = read('apps/web/public/sitemap.xml');
  assert.ok(Array.isArray(manifest.entities), 'empty authoritative publications are valid');
  for (const entity of manifest.entities) {
    assert.ok(sitemap.includes(`${manifest.origin}/entity/${entity.slug}/`));
    assert.ok(entity.metadata.openGraph);
    assert.ok(entity.schema['@context']);
  }
  assert.ok(read('apps/web/public/robots.txt').includes(`Sitemap: ${manifest.origin}/sitemap.xml`));
  assert.equal(read('apps/web/public/styles/globals.css'), read('apps/web/src/styles/globals.css'));
  // Only active App Router documents are indexed; /legacy is a redirect.
  assert.doesNotMatch(sitemap, /\/legacy/, 'sitemap must not list the archived /legacy path');
  assert.match(sitemap, /\/dashboard\//);
});
test('invalid origin fails the build before overwriting SEO', () => {
  const result = spawnSync(process.execPath, ['scripts/generate-seo.js'], {
    encoding: 'utf8', env: { ...process.env, SITE_ORIGIN: 'https://example.com/not-an-origin' },
  });
  assert.notEqual(result.status, 0);
});
test('dashboard and landing colors are token-only; reserved mobile space survives dismissal', () => {
  for (const file of ['apps/web/app/dashboard/dashboard.css', 'apps/web/app/styles.css']) {
    const css = read(file).replace(/\/\*[\s\S]*?\*\//g, '');
    assert.doesNotMatch(css, /#[0-9a-f]{3,8}\b|rgba?\(/i);
  }
  // globals.css is the one place hex values live: the palette token definitions.
  assert.doesNotMatch(read('apps/web/app/styles.css'), /--site-/, 'no legacy palette alias layer');
  assert.doesNotMatch(read('apps/web/src/styles/globals.css'), /--site-/);
  assert.ok(read('apps/web/app/components/AdContainers.tsx').includes('localStorage.setItem'));
  assert.doesNotMatch(read('apps/web/app/styles.css'), /body:has\(\.mobile-ad-container\)/);
});
test('custom SITE_ORIGIN is used consistently across sitemap and Schema.org', () => {
  try {
    const result = spawnSync(process.execPath, ['scripts/generate-seo.js'], {
      encoding: 'utf8', env: { ...process.env, SITE_ORIGIN: 'https://release.example.org', NEXT_PUBLIC_SITE_ORIGIN: 'https://old.example.org' },
    });
    assert.equal(result.status, 0, result.stderr);
    const sitemap = read('apps/web/public/sitemap.xml');
    const manifest = read('apps/web/public/data/seo-metadata.json');
    assert.ok(sitemap.includes('https://release.example.org/'));
    assert.ok(manifest.includes('https://release.example.org'));
    assert.ok(!manifest.includes('https://old.example.org'));
  } finally {
    const reset = spawnSync(process.execPath, ['scripts/generate-seo.js'], { encoding: 'utf8' });
    assert.equal(reset.status, 0, reset.stderr);
  }
});


test('resource defaults avoid unsolicited GPU renderer and force simulation', () => {
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  // Default stays the cheap table view; the network renderer requires an
  // explicit user choice — including a validated ?mode=network permalink
  // (decodeViewState only accepts the whitelisted enum, junk cannot opt in).
  assert.ok(dashboard.includes("useState<'network' | 'map' | 'list'>(initialUrl.current.mode ?? 'list')"));
  assert.ok(dashboard.includes("mode === 'network' && <NetworkGraph"));
  assert.match(read('apps/web/app/dashboard/graph.worker.ts'), /layoutWanted = false/);
  const scripts = JSON.parse(read('apps/web/package.json')).scripts;
  assert.ok(scripts.prebuild.includes('generate-seo.js'));
});

test('an unusable publication becomes standby, never a silent demo swap', () => {
  const fetcher = read('apps/web/src/lib/dataFetcher.ts');
  assert.match(fetcher, /source: 'standby'/);
  // Wave 6: the standby strings live in the UI dictionary; the panel must
  // render exactly those keys (the a11y suite asserts the rendered German).
  const bootState = read('apps/web/app/dashboard/BootState.tsx');
  assert.ok(bootState.includes("t('boot.standbyTitle')"), 'the standby heading is dictionary-driven');
  assert.ok(bootState.includes("t('boot.demoButton')"), 'the demo action is dictionary-driven');
  const strings = read('apps/web/lib/i18n-strings.ts');
  assert.match(strings, /System im Bereitschaftsmodus/);
  assert.match(strings, /Demo-Datensatz laden/);
});

test('CSS primitives and canvas palette share the same source', () => {
  const palette = JSON.parse(read('apps/web/src/styles/palette.json'));
  const tokens = read('apps/web/src/styles/globals.css');
  for (const [key, value] of Object.entries(palette)) assert.ok(tokens.includes(`--color-${key}: ${value};`));
  for (const file of ['NetworkGraph.tsx', 'MapModule.tsx', 'graph-data.ts']) {
    assert.doesNotMatch(read(`apps/web/app/dashboard/${file}`), /#[0-9a-f]{6}\b/i);
  }
});


test('empty entity publication has a reserved 404 route and postbuild cleanup', () => {
  const page = read('apps/web/app/entity/[id]/page.tsx');
  assert.ok(page.includes("profiles.length ? profiles : [{ id: '__empty-publication' }]"));
  assert.ok(page.includes('if (!entity) notFound()'));
  const scripts = JSON.parse(read('apps/web/package.json')).scripts;
  assert.match(scripts.postbuild, /finalize-static-export/);
});

test('the legal page documents the erasure chain and the source policy', () => {
  const page = read('apps/web/app/legal/page.tsx');
  assert.ok(page.includes('public_released: false'), 'the technical erasure chain must be named');
  assert.match(page, /Art\. 17/);
  assert.match(page, /C-37\/20/);
  assert.match(page, /C-601\/20/);
  assert.match(page, /5\/2019/);
  assert.ok(page.includes('lang="en"'), 'the page is bilingual DE+EN');
  // Discoverability: both footers link it, and the sitemap lists it.
  assert.ok(read('apps/web/app/page.tsx').includes('href="/legal/"'));
  assert.ok(read('apps/web/app/dashboard/Dashboard.tsx').includes('href="/legal/"'));
  const result = spawnSync(process.execPath, ['scripts/generate-seo.js'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const origin = JSON.parse(read('apps/web/public/data/seo-metadata.json')).origin;
  assert.ok(read('apps/web/public/sitemap.xml').includes(`${origin}/legal/`));
  const policy = read('docs/source-policy.md');
  assert.match(policy, /C-37\/20/);
  assert.ok(policy.includes('public_released'));
});

test('edge provenance reaches the worker contract and the inspector', () => {
  assert.match(read('apps/web/app/dashboard/protocol.ts'), /kind: 'edgeDetail'; id: string/);
  assert.match(read('apps/web/app/dashboard/graph.worker.ts'), /evidenceForEdge\(dataset, data\.id\)/);
  assert.match(read('apps/web/app/dashboard/NetworkGraph.tsx'), /renderer\.on\('clickEdge'/);
  const panel = read('apps/web/app/dashboard/SidePanel.tsx');
  // Wave 6: the pinned German heading moved into the UI dictionary; the panel
  // must render that key and the dictionary must carry the exact string.
  assert.ok(panel.includes("t('inspector.edgeSources')"), 'the edge inspector heading is dictionary-driven');
  assert.ok(read('apps/web/lib/i18n-strings.ts').includes('Quellen dieser Kante'), 'the pinned German heading survives in the dictionary');
  assert.ok(panel.includes('AGGREGATED'), 'grouped edges explain the LoD limitation instead of inventing sources');
});

test('the PWA shell is safe: guarded registration, honest offline fallback, fresh worker', () => {
  const sw = read('apps/web/public/sw.js');
  assert.match(sw, /const CACHE = 'puppetnet-v1'/);
  assert.match(sw, /request\.method !== 'GET'/, 'only GET requests are intercepted');
  assert.match(sw, /url\.origin !== self\.location\.origin/, 'cross-origin requests pass through untouched');
  assert.match(sw, /url\.pathname\.startsWith\('\/data\/'\)/, 'dataset updates are revalidated (stale-while-revalidate), never pinned stale');
  assert.match(sw, /caches\.match\('\/dashboard\/'\)/, 'offline navigation falls back to the cached app shell');
  assert.match(sw, /status: 503/, 'nothing is fabricated when even the shell is missing');
  assert.match(sw, /skipWaiting/);
  assert.match(sw, /clients\.claim/);
  const manifest = JSON.parse(read('apps/web/public/manifest.webmanifest'));
  assert.equal(manifest.start_url, '/dashboard/');
  assert.equal(manifest.display, 'standalone');
  assert.ok(Array.isArray(manifest.icons) && manifest.icons.length >= 2);
  for (const icon of manifest.icons) assert.ok(existsSync(`apps/web/public${icon.src}`), `manifest icon exists: ${icon.src}`);
  const offline = read('apps/web/app/components/OfflineService.tsx');
  assert.ok(offline.includes("'serviceWorker' in navigator"), 'registration is feature-guarded');
  assert.ok(offline.includes("navigator.serviceWorker.register('/sw.js')"));
  const layout = read('apps/web/app/layout.js');
  assert.ok(layout.includes('<OfflineService />'), 'the registrar is mounted once in the root layout');
  assert.ok(layout.includes("manifest: '/manifest.webmanifest'"), 'the manifest is published via metadata');
  assert.ok(layout.includes('<LanguageProvider>'), 'the UI language context wraps the app');
  assert.match(read('apps/web/public/_headers'), /\/sw\.js\n {2}Cache-Control: no-store/, 'the service worker itself is never cached');
});

test('the English landing is a real, linked, hreflang-annotated publication', () => {
  const en = read('apps/web/app/en/page.tsx');
  assert.ok(en.includes('lang="en"'), 'the English subtree is language-tagged');
  assert.ok(en.includes("canonical: '/en/'"));
  assert.ok(en.includes("languages: { de: '/', en: '/en/', 'x-default': '/' }"));
  const de = read('apps/web/app/page.tsx');
  assert.ok(de.includes("languages: { de: '/', en: '/en/', 'x-default': '/' }"), 'the German landing declares its alternate');
  assert.ok(de.includes('href="/en/"'), 'the German footer links the English landing');
  assert.ok(en.includes('href="/"'), 'the English footer links back to the German original');
  for (const file of ['scripts/generate-sitemap.mjs', 'scripts/generate-seo.js', 'scripts/emit-seo-files.mjs']) {
    assert.ok(read(file).includes('/en/'), `${file} lists the English landing`);
  }
});

test('wave 7: ego expansion and temporal analytics stay inside the worker contract', () => {
  assert.match(read('apps/web/app/dashboard/protocol.ts'), /kind: 'ego'; version: number; id: string; depth: number; filters: Filters/);
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /egoNetwork\(filterDataset\(effectiveDataset\(\), data\.filters\), data\.id, data\.depth\)/, 'ego runs on the filtered slice in the worker');
  assert.match(worker, /mentionTimeline\(filtered\.nodes\)/, 'the timeline is computed pre-grouping');
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export const EGO_DEPTH_LIMIT = 3/);
  assert.match(data, /export function egoNetwork/);
  assert.match(data, /export function mentionTimeline/);
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.egoTitle')"), 'the ego panel is dictionary-driven');
  assert.ok(dashboard.includes("t('dash.timelineTitle')"), 'the timeline block is dictionary-driven');
  assert.ok(read('apps/web/app/dashboard/SidePanel.tsx').includes("t('inspector.egoButton')"), 'the inspector offers expansion');
});

test('wave 8: entity-resolution hints stay browser-side and never auto-merge', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function duplicateCandidates\(nodes: GraphNode\[\], limit = 12\)/, 'bounded, pure hint function');
  assert.match(data, /export function monthWindow\(month: string\)/, 'timeline clicks resolve through the real calendar');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /duplicateCandidates\(filtered\.nodes\)/, 'hints are computed on the filtered slice in the worker');
  assert.match(worker, /\.\.\.\(duplicates\.length \? \{ duplicates \} : \{\}\)/, 'empty hint lists are omitted, not faked');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.dupeTitle')"), 'duplicate block is dictionary-driven');
  assert.ok(dashboard.includes('aria-label={t(\'dash.timelineSelect\''), 'timeline months are labelled controls');
  assert.ok(dashboard.includes('setSearch(group.label)'), 'comparing duplicates only switches the table query');
  assert.ok(!/merge|dedupe\(/.test(data.match(/export function duplicateCandidates[\s\S]*?\n}/)?.[0] ?? ''), 'no silent merging');
});

test('wave 9: bundling and vulnerability analysis stay deterministic and browser-side', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function bundleEdges\(edges: GraphEdge\[\]\): GraphEdge\[\]/, 'bundling is a pure function');
  assert.match(data, /export function structuralVulnerability\(nodes: GraphNode\[\], edges: GraphEdge\[\], limit = 12, maxNodes = VULNERABILITY_NODE_LIMIT\)/, 'bounded, injectable node limit');
  assert.match(data, /export const VULNERABILITY_NODE_LIMIT = 50_000/);
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /const vulnerability = structuralVulnerability\(filtered\.nodes, filtered\.edges\)/, 'the worker analyses the visible slice');
  assert.match(worker, /const viewEdges = filters\.bundle \? bundleEdges\(grouped\.edges\) : grouped\.edges/, 'bundling is opt-in');
  assert.match(worker, /\.\.\.\(vulnerability \? \{ vulnerability \} : \{\}\)/, 'no fabricated analytics blocks');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.bundleLabel')"), 'bundling toggle is dictionary-driven');
  assert.ok(dashboard.includes("t('dash.vulnTitle')"), 'vulnerability block is dictionary-driven');
  assert.ok(dashboard.includes("t('dash.vulnCutEdges')"), 'cut edges are explained, not just counted');
});

test('wave 10: path chronology and failure simulation stay honest', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function pathTimeline\(data: Dataset, ids: string\[\]\): PathTimeline \| null/, 'pure, null-honest path chronology');
  assert.match(data, /const componentsWithout = \(skip: number\)/, 'failure simulation reuses the same adjacency');
  assert.match(data, /impact: componentsWithout\(index\.get\(entry\.id\)!\)/, 'impact is attached to the bounded articulation list');
  assert.match(read('apps/web/app/dashboard/protocol.ts'), /timeline: PathTimeline \| null/, 'the worker ships the hop timeline');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /timeline: result \? pathTimeline\(slice, result\.nodes\) : null/, 'the worker derives chronology from the visible slice');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.pathTimelineTitle')"), 'the per-hop block is dictionary-driven');
  assert.ok(dashboard.includes("t('dash.pathChronoOk')"), 'a plausible chain is stated positively');
  assert.ok(dashboard.includes("t('dash.vulnImpact',"), 'the failure simulation is shown next to each articulation point');
  assert.ok(!/Math\.random/.test(data.match(/export function pathTimeline[\s\S]*?\n}/)?.[0] ?? ''), 'no randomness in chronology');
});

test('wave 11: research notes stay local and the case report stays reproducible', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export const NOTE_STORAGE_KEY = 'puppetnet\.notes\.v1'/, 'notes use a versioned local key');
  assert.match(data, /export const NOTE_LIMIT = 2_000/);
  assert.match(data, /export function readNotes\(raw: unknown\): Record<string, string>/, 'stored notes are sanitized, never trusted');
  assert.match(data, /export function caseReport\(input: CaseReportInput, generated: string, lang: 'de' \| 'en' = 'de'\)/, 'the report takes its timestamp as an argument');
  assert.doesNotMatch(data.match(/export function caseReport[\s\S]*?\n}/)?.[0] ?? '', /Date\.now|Math\.random/, 'no hidden nondeterminism in the report');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes('window.localStorage.getItem(NOTE_STORAGE_KEY)'), 'notes load from this browser only');
  assert.ok(!/fetch\([^)]*notes/i.test(dashboard), 'notes are never uploaded');
  assert.ok(dashboard.includes("t('dash.reportButton')"), 'the report export is dictionary-driven');
  assert.ok(read('apps/web/app/dashboard/SidePanel.tsx').includes("t('inspector.noteHint')"), 'the local-only promise is stated in the inspector');
});
