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

test('wave 12: cumulative playback and evidence windows stay browser-side', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function nextPlaybackMonth\(months: string\[\], current: string \| undefined, direction: 1 \| -1\): string \| null/, 'playback stepping is pure');
  assert.match(data, /export function evidenceWindow\(evidence: Evidence\[\]\)/, 'edge windows are pure');
  assert.doesNotMatch(data.match(/export function nextPlaybackMonth[\s\S]*?\n}/)?.[0] ?? '', /Date\.now|Math\.random/, 'steps never invent time');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.match(dashboard, /setInterval\(\(\) => stepPlaybackRef\.current\(1\), 1_400\)/, 'playback ticks through the frozen month list');
  assert.ok(dashboard.includes("t('dash.playStart')"), 'playback controls are dictionary-driven');
  assert.ok(dashboard.includes("t('dash.playNote')"), 'the cumulative semantics are explained in the UI');
  assert.ok(read('apps/web/app/dashboard/SidePanel.tsx').includes("t('inspector.window')"), 'the edge inspector shows its documented window');
});

test('wave 13: entity resolution compares, the app never merges, failure tests stay sliced', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function compareDuplicates\(data: Dataset, ids: string\[\], neighborLimit = 8\): DuplicateComparison \| null/, 'comparison is pure and null-honest');
  assert.match(data, /export function multiFailureImpact\(nodes: GraphNode\[\], edges: GraphEdge\[\], ids: string\[\], maxNodes = 4\)/, 'the failure test is bounded');
  assert.doesNotMatch(data.match(/export function compareDuplicates[\s\S]*?\n}/)?.[0] ?? '', /merge|dedupe/, 'comparison never merges ids');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /const slice = currentFiltered\(lastFilters\);\n      const impact = multiFailureImpact\(slice\.nodes, slice\.edges, data\.ids\)/, 'impact runs on the visible slice');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.dupeCompareTitle')"), 'the comparison table is dictionary-driven');
  assert.ok(dashboard.includes("t('dash.dupeCompareNote')"), 'the no-merge rule is stated to the user');
  assert.ok(dashboard.includes("t('dash.impactSelect')"), 'the failure selection is labelled');
});

test('wave 14: saved sessions and the provenance export never leave the browser', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export const SAVED_VIEW_STORAGE_KEY = 'puppetnet\.views\.v1'/, 'sessions use a versioned local key');
  assert.match(data, /export function readSavedViews\(raw: unknown\): SavedView\[\]/, 'stored sessions are sanitized, never trusted');
  assert.match(data, /export function upsertSavedView\(list: SavedView\[\], name: string, query: string, saved: string\): SavedView\[\]/, 'saving is pure so it can be tested');
  assert.match(data, /export function evidenceToCSV\(data: Dataset, nodes: GraphNode\[\], edges: GraphEdge\[\], limit = 5_000\)/, 'the export is bounded');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /const csv = evidenceToCSV\(slice, slice\.nodes, slice\.edges\)/, 'the worker exports the visible slice only');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes('window.localStorage.getItem(SAVED_VIEW_STORAGE_KEY)'), 'sessions load from this browser only');
  assert.ok(dashboard.includes("t('dash.viewsTitle')"), 'the sessions block is dictionary-driven');
  assert.ok(dashboard.includes("t('dash.evidenceCsvAria')"), 'the evidence export is labelled');
  assert.ok(!/fetch\([^)]*savedViews/i.test(dashboard), 'saved sessions are never uploaded');
});

test('wave 15: case-report notes round-trip locally, conflicts are never overwritten', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function parseCaseNotes\(markdown: string\): Record<string, string>/, 'the parser is pure and typed');
  assert.match(data, /export function mergeNotes\(current: Record<string, string>, imported: Record<string, string>\)/, 'merging is pure');
  assert.match(data, /if \(existing === note\) \{ unchanged\+\+; continue; \}\s+skipped\+\+;/, 'a differing local note is kept, not replaced');
  assert.doesNotMatch(data.match(/export function parseCaseNotes[\s\S]*?\n}/)?.[0] ?? '', /fetch|XMLHttpRequest/, 'imported reports never leave the device');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes('parseCaseNotes(await file.text())'), 'the file is read in the browser');
  assert.ok(dashboard.includes("t('dash.notesImported'"), 'the import summary is announced');
  assert.ok(dashboard.includes("t('dash.notesImportEmpty')"), 'a foreign file is answered honestly');
});

test('wave 16: the note index and the case bundle stay local and reproducible', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function searchNoteIndex\(nodes: GraphNode\[\], notes: Record<string, string>, query: string, limit = 20\)/, 'the note index is pure and bounded');
  assert.match(data, /export function caseBundle\(input: CaseBundleInput\): string/, 'the bundle is a pure function');
  assert.match(data, /format: 'puppetnet-case'/, 'the bundle carries a versioned format marker');
  assert.doesNotMatch(data.match(/export function caseBundle[\s\S]*?\n}/)?.[0] ?? '', /Date\.now|Math\.random/, 'no hidden nondeterminism in the bundle');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.notesSearch')"), 'the note index is dictionary-driven');
  assert.ok(dashboard.includes("t('dash.caseBundleAria')"), 'the bundle export is labelled');
  assert.ok(!/fetch\([^)]*caseBundle/i.test(dashboard), 'the bundle is generated locally, never uploaded');
});

test('wave 17: provenance gaps are computed in the worker and bundles reject junk', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function provenanceGaps\(nodes: GraphNode\[\], edges: GraphEdge\[\], limit = 8\): ProvenanceGaps/, 'gap analysis is pure and bounded');
  assert.match(data, /export const CONFIDENCE_THRESHOLD = 0\.5/, 'the single-source rule has a named threshold');
  assert.match(data, /export function readCaseBundle\(raw: unknown\)/, 'bundles are parsed defensively');
  assert.match(data, /if \(bundle\.format !== 'puppetnet-case' \|\| Number\(bundle\.version\) !== 1\) return null;/, 'wrong formats and versions are rejected whole');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /const gaps = provenanceGaps\(filtered\.nodes, grouped\.edges\)/, 'gaps are computed on the visible slice');
  assert.match(worker, /\.\.\.\(vulnerability \? \{ vulnerability \} : \{\}\), gaps,/, 'coverage travels with every view, even when clean');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.gapsTitle')"), 'the gap panel is dictionary-driven');
  assert.ok(dashboard.includes("t('dash.gapsNone')"), 'a clean slice is stated positively');
  assert.ok(dashboard.includes('parseCaseNotes') && dashboard.includes('readCaseBundle'), 'both import paths exist');
});

test('wave 18: source independence is judged, not just counted', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function edgeSources\(evidence: Evidence\[\]\): EdgeSources/, 'the verdict is a pure function');
  assert.match(data, /export function sharedSourceGroups\(nodes: GraphNode\[\], evidence: Evidence\[\], limit = 8\)/, 'shared documents are grouped purely');
  assert.match(data, /documents === 1 \? 'single'/, 'a single document is never called corroborated');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /const sharedSources = sharedSourceGroups\(filtered\.nodes, dataset\.evidence\)/, 'shared documents come from the visible slice and the real evidence table');
  assert.match(worker, /\.\.\.\(sharedSources\.length \? \{ sharedSources \} : \{\}\)/, 'empty groups are omitted');
  const panel = read('apps/web/app/dashboard/SidePanel.tsx');
  assert.ok(panel.includes("t('inspector.sourcesIndependent'"), 'the inspector states independent corroboration explicitly');
  assert.ok(panel.includes("t('inspector.sourcesShared'"), 'same-outlet evidence is called out instead of counted twice');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.sharedText')"), 'the shared-document panel carries its no-identity-claim note');
});

test('wave 19: the evidence-quality filter is shareable, bounded and honest', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export type SourceFilter = 'any' \| 'corroborated' \| 'thin'/, 'the filter is a closed vocabulary');
  assert.match(data, /export function edgeSourceStatus\(edge: GraphEdge, evidence: Evidence\[\]\): SourceIndependence/, 'per-edge status is reusable and pure');
  assert.match(data, /if \(filters\.sources === 'corroborated'\) edges = edges\.filter/, 'corroboration narrows edges only');
  assert.match(data, /const SOURCE_FILTERS: SourceFilter\[\] = \['corroborated', 'thin'\]/, 'only known values survive a round trip');
  assert.match(data, /if \(sources && SOURCE_FILTERS\.includes\(sources\)\) state\.sources = sources/, 'URL junk never reaches the filter');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.sourcesText')"), 'the claim-not-entity rule is stated in the UI');
  assert.ok(dashboard.includes("t('dash.sourcesEmpty')"), 'an empty narrowing is explained, not hidden');
  assert.match(dashboard, /sources: filters\.sources/, 'permalink state carries the filter');
});

test('wave 20: the source registry and outlet filter stay deterministic and honest', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function sourceRegistry\(data: Dataset, nodes: GraphNode\[\], edges: GraphEdge\[\], limit = 12\)/, 'the registry is pure and bounded');
  assert.match(data, /for \(const docId of \[\.\.\.uses\.keys\(\)\]\.sort\(\)\)/, 'documents are visited in id order, so evidence order cannot change the registry');
  assert.match(data, /const outlet = \(filters\.outlet \?\? ''\)\.trim\(\)\.toLowerCase\(\)\.slice\(0, 80\)/, 'outlet names are normalised and bounded');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /const registrySlice = filters\.outlet \? filterDataset\(effectiveDataset\(\), \{ \.\.\.filters, outlet: undefined \}\) : filtered/, 'the registry always lists every outlet, even while one is active');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.registryTitle')"), 'the registry is dictionary-driven');
  assert.ok(dashboard.includes("t('dash.registryUnnamed')"), 'documents without a source are named, not dropped');
  assert.ok(dashboard.includes("t('dash.registryClear')"), 'the outlet filter can always be undone');
});

test('wave 21: contradictions and currency are judged, never guessed', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function pairConflicts\(nodes: GraphNode\[\], edges: GraphEdge\[\], evidence: Evidence\[\], limit = 8\): PairConflict\[\]/, 'conflict detection is pure and bounded');
  assert.match(data, /const overlapping = claims\.some/, 'overlap decides between conflict and development');
  assert.match(data, /export function monthsBefore\(reference: string, months: number\): string \| null/, 'date arithmetic is a testable pure helper');
  assert.match(data, /export function evidenceFreshness\(nodes: GraphNode\[\], evidence: Evidence\[\], reference: string, months = 12, limit = 8\): Freshness \| null/, 'freshness takes its reference date from outside');
  assert.doesNotMatch(data.match(/export function evidenceFreshness[\s\S]*?\n}/)?.[0] ?? '', /Date\.now/, 'no hidden clock in the analysis');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /const conflicts = pairConflicts\(\w+\.nodes, \w+\.edges, dataset\.evidence\)/, 'conflicts run on a visible slice');
  assert.match(worker, /evidenceFreshness\([\s\S]{0,80}new Date\(\)\.toISOString\(\)\.slice\(0, 10\)/, 'the UI passes today as a plain day');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.conflictText')"), 'the conflict panel explains development vs contradiction');
  assert.ok(dashboard.includes("t('dash.freshText',"), 'the currency panel explains unknown-is-not-old');
  assert.ok(dashboard.includes("t('dash.freshNone')"), 'a current case is stated positively');
});

test('wave 22: the dispute view and its window are bounded and reversible', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export const FRESH_WINDOWS = \[6, 12, 24, 60\] as const/, 'the window is a closed vocabulary');
  assert.match(data, /if \(filters\.dispute\) \{/, 'the dispute narrowing is part of the one filter function');
  assert.match(data, /\.filter\(entry => entry\.kind === 'conflict'\)/, 'only real conflicts narrow the network, developments do not');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  const analysisSlice = /const analysisSlice = [^;]+/.exec(worker)?.[0] ?? '';
  assert.ok(analysisSlice.includes('dispute: undefined') && analysisSlice.includes('filterDataset(effectiveDataset()'),
    'conflicts are judged before the dispute view narrows, so the panel stays complete');
  assert.match(worker, /isFreshWindow\(filters\.fresh\) \? filters\.fresh : DEFAULT_FRESH_MONTHS/, 'an unknown window falls back to the default instead of guessing');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.conflictShowAll')"), 'the view can always be undone');
  assert.ok(dashboard.includes("t('dash.conflictNoneInScope')"), 'an empty dispute set is stated honestly');
  assert.ok(dashboard.includes('FRESH_WINDOWS.map('), 'the window is switchable in the panel');
  assert.ok(dashboard.includes('aria-pressed={view.freshness?.months === months}'), 'the active window is exposed to assistive tech');
});

test('wave 23: backing is one definition, and the panel outlives its own filter', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function nodeBacking\(nodes: GraphNode\[\], edges: GraphEdge\[\], evidence: Evidence\[\]\): Map<string, \{ documents: number; sources: number; newest\?: string \}>/,
    'the counting rule lives in exactly one exported place');
  assert.match(data, /if \(filters\.weak\) \{[\s\S]{0,220}nodeBacking\(nodes, edges, data\.evidence\)/, 'the filter reuses that one definition');
  assert.doesNotMatch(data.match(/if \(filters\.weak\)[\s\S]{0,400}?\n  \}/)?.[0] ?? '', /new Date\(\)/,
    'the thin-file filter carries no clock, so it is reproducible');
  assert.match(data, /export function evidenceRisk\(nodes: GraphNode\[\], edges: GraphEdge\[\], evidence: Evidence\[\], reference: string, months = 12, limit = 8\): RiskReport \| null/, 'risk is pure and takes its reference date');
  assert.match(data, /export function riskToCSV\(report: RiskReport\): string/, 'the follow-up list is exportable');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /const narrowed = filters\.dispute \|\| filters\.weak;/, 'both narrowing views share one pre-filter slice');
  assert.match(worker, /evidenceRisk\(analysisSlice\.nodes, analysisSlice\.edges, dataset\.evidence/, 'the risk panel is judged before narrowing');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.riskText')"), 'the panel explains the two dimensions and the unnamed-document rule');
  assert.ok(dashboard.includes("t('dash.riskNone')"), 'an all-solid file is stated positively');
  assert.ok(dashboard.includes('riskToCSV(view.risk!)'), 'the CSV button is wired');
  assert.ok(dashboard.includes('aria-pressed') || dashboard.includes("t('dash.riskOnlyWeakAria')"), 'the toggle is announced');
});

test('wave 24: the comparison is strict about identity and parsed off the UI thread', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function diffDatasets\(previous: Dataset, current: Dataset, limit = 6\): DatasetDiff/, 'the diff is pure and bounded');
  assert.match(data, /const relationKey = \(edge: GraphEdge\) => `\$\{edge\.source\}\|\$\{edge\.type\}\|\$\{edge\.target\}`/, 'relationships are identified by the directed triple');
  assert.match(data, /export function diffToCSV\(diff: DatasetDiff\): string/, 'the change list is exportable');
  assert.doesNotMatch(data.match(/export function diffDatasets[\s\S]*?\n}/)?.[0] ?? '', /new Date\(\)/, 'no hidden clock in a comparison');
  const worker = read('apps/web/app/dashboard/graph.worker.ts');
  assert.match(worker, /if \(data\.kind === 'diff'\) \{[\s\S]{0,900}const baseline = normalizeDataset/, 'the baseline is parsed in the worker, like every other import');
  assert.match(worker, /diffDatasets\(baseline, dataset\)/, 'the diff is computed against the dataset on screen');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.diffLoad')"), 'the comparison state is loadable');
  assert.ok(dashboard.includes("t('dash.diffNone')"), 'an identical state is stated honestly');
  assert.ok(dashboard.includes('diffToCSV(diff)'), 'the CSV button is wired');
  assert.ok(dashboard.includes("t('dash.diffText')"), 'the panel explains the identity rules');
  assert.ok(dashboard.includes('setDiff(null)'), 'the comparison can be removed');
});

test('wave 25: the case search is one ranked index, not a second search box', () => {
  const data = read('apps/web/app/dashboard/graph-data.ts');
  assert.match(data, /export function searchCase\(query: string, input: \{/, 'the search takes one explicit input object');
  assert.match(data, /const CASE_WEIGHT: Record<CaseKind, number> = \{ entity: 50, conflict: 40, risk: 30, outlet: 20, note: 10 \}/,
    'kind weighting is declared, not scattered');
  assert.match(data, /if \(!q\) return \[\];/, 'an empty query returns nothing instead of dumping the case');
  assert.match(data, /export function noteSnippet\(text: string, query: string, size = 90\): string/, 'excerpts are a tested pure helper');
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("t('dash.caseSearchHint')"), 'the panel says exactly what is searched');
  assert.ok(dashboard.includes("t('dash.caseSearchEmpty')"), 'a miss explains filters as a possible cause');
  assert.ok(dashboard.includes("t('dash.caseKindConflict')") === false, 'kind labels are derived, not hard-coded per row');
  assert.match(dashboard, /t\(`dash\.caseKind\$\{hit\.kind\[0\]\.toUpperCase\(\)\}/, 'kind labels come from the closed vocabulary');
  assert.ok(dashboard.includes("setFilters(f => ({ ...f, dispute: true }))"), 'a contradiction hit jumps into the dispute view');
  assert.ok(dashboard.includes("outlet: hit.id"), 'a source hit applies the outlet filter');
  assert.ok(dashboard.includes('searchCase(caseQuery, { nodes: view.nodes, conflicts: view.conflicts, risk: view.risk, sourceRegistry: view.sourceRegistry, notes })'),
    'the index is built from the same view data the panels use');
});

test('wave 26: the build gate runs from the root or from a workspace root', () => {
  for (const [file, script] of [['apps/web/package.json', 'ci:pages'], ['apps/worker/package.json', 'ci:worker']]) {
    const pkg = JSON.parse(read(file));
    assert.equal(pkg.scripts[script], `cd ../.. && npm run ${script}`,
      `${file} delegates the gate to the repository root, so a build started in the workspace root works`);
  }
  for (const [file, marker] of [['scripts/pages-build.mjs', 'dir'], ['scripts/worker-build.mjs', 'dir']]) {
    const source = read(file);
    assert.match(source, /const repoRoot = resolve\(dirname\(fileURLToPath\(import\.meta\.url\)\), '\.\.'\)/, `${file} derives its repository root`);
    assert.match(source, /process\.chdir\(repoRoot\)/, `${file} switches to that root before touching files`);
  }
  const settings = JSON.parse(read('cloudflare-builds.json'));
  assert.equal(settings.pages.rootDirectory, 'apps/web');
  assert.equal(settings.pages.buildCommand, 'npm run ci:pages', 'the documented command is the workspace-safe form');
  assert.equal(settings.worker.buildCommand, 'npm run ci:worker');
  assert.ok(read('docs/operations.md').includes('Missing script: "ci:pages"'), 'the operations note names the failure signature');
});
