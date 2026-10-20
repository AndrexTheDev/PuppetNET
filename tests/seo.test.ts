import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import {
  MAX_RELATED_ENTITIES,
  buildEntityJsonLd,
  buildEntityMetadata,
  buildWebsiteJsonLd,
  entityPath,
  entitySlug,
  normalizeEntityDataset,
  serializeJsonLd,
  siteOrigin,
} from '../apps/web/lib/entity-seo';
import {
  findEntityBySlug,
  listPublicEntities,
  loadEntityDataset,
  resetEntityCache,
} from '../apps/web/lib/entity-store';
import {
  entityEntries,
  escapeXml,
  normalizeOrigin,
  renderSitemaps,
  renderUrlset,
} from '../scripts/generate-sitemap.mjs';
import { buildEgoGraphSvg } from '../apps/web/lib/ego-graph';

const DATA = mkdtempSync(path.join(tmpdir(), 'puppetnet-seo-fixture-'));
writeFileSync(path.join(DATA, 'entities.sample.json'), readFileSync('apps/web/public/data/entities.sample.json'));
after(() => rmSync(DATA, { recursive: true, force: true }));
const ORIGIN = 'https://puppetnet-web.pages.dev';

function sample() {
  return loadEntityDataset(DATA);
}

test('the demo dataset publishes only public, named entities', () => {
  const { dataset, source } = sample();
  assert.equal(source, 'sample');
  assert.equal(dataset.demo, true);
  const ids = dataset.entities.map((entity) => entity.id);
  assert.ok(ids.includes('org:nordlicht-holdings'));
  assert.ok(!ids.includes('doc:demo-internal'), 'a public:false document must not get a page');
  assert.ok(!ids.includes('org:unlisted-vehicle-holding'), 'a noindex entity must not get a page');
  assert.equal(dataset.entities.length, 8);
  assert.ok(dataset.entities.every((entity) => entity.slug === entitySlug(entity.id, entity.name)));
});

test('slugs are stable, URL-safe and collision-resistant', () => {
  assert.equal(entitySlug('per:anna-berg', 'Anna Berg'), entitySlug('per:anna-berg', 'Anna Berg'));
  assert.notEqual(entitySlug('per:anna-berg', 'Anna Berg'), entitySlug('per:anna-berg-2', 'Anna Berg'));
  assert.match(entitySlug('x', 'Müller & Söhne GmbH'), /^[a-z0-9][a-z0-9-]{0,80}$/);
  assert.match(entitySlug('x', 'ООО Ромашка'), /^entity-[a-z0-9]+$/);
  assert.equal(entityPath('anna-berg-08vgf4h'), '/entity/anna-berg-08vgf4h/');
});

test('relationships stay inside the published page set', () => {
  const { dataset } = sample();
  const published = new Set(dataset.entities.map((entity) => entity.id));
  for (const entity of dataset.entities) {
    assert.ok(entity.related.length <= MAX_RELATED_ENTITIES);
    for (const related of entity.related) {
      assert.ok(published.has(related.id), `${related.id} is linked but has no page`);
      assert.ok(dataset.entities.some((item) => item.slug === related.slug));
    }
  }
  const nordlicht = dataset.entities.find((entity) => entity.id === 'org:nordlicht-holdings')!;
  assert.ok(nordlicht.related.some((item) => item.predicate === 'OWNS' && item.direction === 'out'));
  const anna = dataset.entities.find((entity) => entity.id === 'per:anna-berg')!;
  assert.ok(anna.related.some((item) => item.id === 'org:nordlicht-holdings' && item.direction === 'out'));
  const hans = dataset.entities.find((entity) => entity.id === 'org:hansa-maritime')!;
  assert.ok(hans.related.some((item) => item.id === 'org:nordlicht-holdings' && item.direction === 'in'));
});

test('graph exports are normalised without inventing facts', () => {
  const dataset = normalizeEntityDataset({
    generatedAt: '2026-01-02',
    nodes: [
      { key: 'a', name: 'Beispiel GmbH', entity_type: 'org', confidence: 0.8, reg_number: 'HRB 1', citations: [{ id: 'd1', title: 'Register', url: 'https://example.org/a' }] },
      { key: 'b', name: 'Beispiel GmbH', entity_type: 'org', confidence: 0.4 },
      { key: 'c', name: 'Privat', entity_type: 'person', public: false },
      { key: 'd', name: 'Dokument', entity_type: 'document' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'owes' },
      { source: 'a', target: 'c', type: 'OWNS' },
    ],
  });
  assert.equal(dataset.entities.length, 2);
  assert.equal(dataset.demo, false);
  const [first, second] = dataset.entities;
  assert.equal(first.description.includes('0.8'), false);
  assert.ok(first.description.includes('Confidence 80 %'));
  assert.ok(first.description.includes('1 Quellenreferenz'));
  assert.equal(first.identifiers[0].property, 'Handelsregister');
  assert.ok(first.related.some((item) => item.predicate === 'OWES' && item.id === second.id));
  assert.ok(second.related.some((item) => item.direction === 'in'));
});

test('a shared canonical key is published once', () => {
  const dataset = normalizeEntityDataset({
    nodes: [
      { id: 'a', name: 'A', type: 'Organization' },
      { id: 'a', name: 'A duplicate', type: 'Organization' },
      { id: 'b', name: 'ООО', type: 'Organization' },
      { id: 'c', name: 'ООО', type: 'Organization' },
    ],
  });
  assert.equal(dataset.entities.length, 3);
  const slugs = dataset.entities.map((entity) => entity.slug);
  assert.equal(new Set(slugs).size, slugs.length, 'two non-Latin names must not share a URL');
});

test('the landing-page WebSite JSON-LD has Schema.org type and an absolute canonical URL', () => {
  const website = buildWebsiteJsonLd(ORIGIN);
  assert.equal(website['@type'], 'WebSite');
  assert.equal(website['@id'], `${ORIGIN}/#website`);
  assert.equal(website.url, `${ORIGIN}/`);
  assert.equal(website.name, 'PuppetNET');
  assert.equal(website.inLanguage, 'de');
});

test('JSON-LD uses Schema.org Person and Organization and absolute URLs', () => {
  const dataset = sample().dataset;
  const anna = dataset.entities.find((entity) => entity.id === 'per:anna-berg')!;
  const person = buildEntityJsonLd(anna, ORIGIN);
  const personNode = (person['@graph'] as Array<Record<string, unknown>>)[1];
  assert.equal(personNode['@type'], 'Person');
  assert.equal(personNode.url, `${ORIGIN}/entity/${anna.slug}/`);
  assert.equal((personNode.address as Record<string, unknown>).addressCountry, 'Deutschland');
  assert.equal((person['@graph'] as unknown[]).length, 3);

  const org = dataset.entities.find((entity) => entity.id === 'org:nordlicht-holdings')!;
  const organization = buildEntityJsonLd(org, ORIGIN);
  const organizationNode = (organization['@graph'] as Array<Record<string, unknown>>)[1];
  assert.equal(organizationNode['@type'], 'Organization');
  assert.equal(organizationNode['@id'], `${ORIGIN}/entity/${org.slug}/#entity`);
  const identifiers = organizationNode.identifier as Array<Record<string, string>>;
  assert.equal(identifiers[0].propertyID, 'Handelsregister');

  const location = dataset.entities.find((entity) => entity.type === 'Location')!;
  assert.equal((buildEntityJsonLd(location, ORIGIN)['@graph'] as Array<Record<string, unknown>>)[1]['@type'], 'Place');
  const craft = dataset.entities.find((entity) => entity.type === 'Craft')!;
  assert.equal((buildEntityJsonLd(craft, ORIGIN)['@graph'] as Array<Record<string, unknown>>)[1]['@type'], 'Vehicle');
});

test('JSON-LD cannot break out of its script element', () => {
  const dataset = normalizeEntityDataset({
    nodes: [{ id: 'x', name: '</script><script>alert(1)</script>', type: 'Organization', description: 'A & B < C' }],
  });
  const entity = dataset.entities[0];
  const serialized = serializeJsonLd(buildEntityJsonLd(entity, ORIGIN));
  assert.equal(serialized.includes('</script>'), false);
  assert.equal(serialized.includes('<'), false);
  assert.equal(serialized.includes('&'), false);
  assert.equal(JSON.parse(serialized)['@context'], 'https://schema.org');
});

test('metadata carries canonical, Open Graph and Twitter tags', () => {
  const dataset = sample().dataset;
  const anna = dataset.entities.find((entity) => entity.id === 'per:anna-berg')!;
  const metadata = buildEntityMetadata(anna);
  assert.equal(metadata.title, 'Anna Berg — Person');
  assert.equal(metadata.alternates?.canonical, `/entity/${anna.slug}/`);
  assert.deepEqual(metadata.robots, { index: true, follow: true, 'max-image-preview': 'large', 'max-snippet': -1 });
  const openGraph = metadata.openGraph as Record<string, unknown>;
  assert.equal(openGraph.type, 'profile');
  assert.equal(openGraph.locale, 'de_DE');
  const images = openGraph.images as Array<Record<string, unknown>>;
  assert.equal(images[0].url, '/og/person.png');
  assert.equal(images[0].width, 1200);
  assert.equal(images[0].height, 630);
  assert.equal((metadata.twitter as Record<string, unknown>).card, 'summary_large_image');

  const org = dataset.entities.find((entity) => entity.type === 'Organization')!;
  assert.equal((buildEntityMetadata(org).openGraph as Record<string, unknown>).type, 'website');
});

test('siteOrigin ignores invalid deployment values instead of emitting them', () => {
  const previous = process.env.NEXT_PUBLIC_SITE_ORIGIN;
  try {
    process.env.NEXT_PUBLIC_SITE_ORIGIN = 'https://example.org/';
    assert.equal(siteOrigin(), 'https://example.org');
    process.env.NEXT_PUBLIC_SITE_ORIGIN = 'https://example.org/path';
    assert.equal(siteOrigin(), 'https://puppetnet-web.pages.dev');
    process.env.NEXT_PUBLIC_SITE_ORIGIN = 'javascript:alert(1)';
    assert.equal(siteOrigin(), 'https://puppetnet-web.pages.dev');
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_SITE_ORIGIN;
    else process.env.NEXT_PUBLIC_SITE_ORIGIN = previous;
  }
});

test('the store reads the publication first and falls back to the demo', () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'puppetnet-seo-'));
  try {
    resetEntityCache();
    writeFileSync(path.join(directory, 'entities.sample.json'), JSON.stringify({ demo: true, entities: [{ id: 'a', name: 'Demo', type: 'Person' }] }));
    assert.equal(loadEntityDataset(directory).source, 'sample');

    writeFileSync(path.join(directory, 'entities.json'), JSON.stringify({ generatedAt: '2026-02-03', entities: [{ id: 'b', name: 'Published', type: 'Organization' }] }));
    resetEntityCache();
    const published = loadEntityDataset(directory);
    assert.equal(published.source, 'export');
    assert.equal(published.dataset.demo, false);
    assert.equal(published.dataset.entities[0].name, 'Published');
    assert.equal(findEntityBySlug(published.dataset.entities[0].slug, directory)?.id, 'b');
    assert.equal(findEntityBySlug('../../etc/passwd', directory), undefined);
    assert.equal(listPublicEntities(directory).length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
    resetEntityCache();
  }
});

/* ------------------------------------------------------------------------- */
/* sitemap                                                                   */
/* ------------------------------------------------------------------------- */

test('sitemap XML escapes text instead of trusting entity names', () => {
  assert.equal(escapeXml(`a & b < c > d "e" 'f'`), 'a &amp; b &lt; c &gt; d &quot;e&quot; &apos;f&apos;');
});

test('the sitemap origin must be a bare http(s) origin', () => {
  assert.equal(normalizeOrigin('https://example.org'), 'https://example.org');
  assert.equal(normalizeOrigin('https://example.org/'), 'https://example.org');
  for (const value of ['ftp://example.org', 'https://example.org/path', 'https://example.org/?a=1', 'not a url']) {
    assert.throws(() => normalizeOrigin(value));
  }
});

test('entityEntries lists only public, well-formed profile URLs', () => {
  const entries = entityEntries([
    { slug: 'anna-berg-08vgf4h', type: 'Person', updatedAt: '2026-10-15' },
    { slug: 'hansa-maritime-ag-1c6ajsd', type: 'Organization', updatedAt: '2026-10-12' },
    { slug: 'anna-berg-08vgf4h', type: 'Person' },
    { slug: 'PRIVATE', type: 'Person' },
    { slug: '../etc/passwd', type: 'Person' },
    { slug: 'unlisted-1', type: 'Organization', public: false },
    { slug: 'noindex-1', type: 'Person', noindex: true },
  ], '2026-10-19');
  assert.deepEqual(entries.map((entry: { loc: string }) => entry.loc), [
    '/entity/anna-berg-08vgf4h/',
    '/entity/hansa-maritime-ag-1c6ajsd/',
  ]);
  assert.equal(entries[0].priority, '0.7');
  assert.equal(entries[0].lastmod, '2026-10-15');
});

test('a small site is one sitemap and a large one is an index plus children', () => {
  const small = renderSitemaps({
    origin: ORIGIN,
    entries: [{ loc: '/' }, { loc: '/dashboard/' }, { loc: '/entity/a-1/' }],
    lastmod: '2026-10-19',
  });
  assert.equal(small.files.length, 1);
  assert.equal(small.files[0].name, 'sitemap.xml');
  assert.match(small.files[0].xml, /<loc>https:\/\/puppetnet-web\.pages\.dev\/entity\/a-1\/<\/loc>/);

  const large = renderSitemaps({
    origin: ORIGIN,
    entries: Array.from({ length: 5 }, (_, index) => ({ loc: `/entity/e-${index}/` })),
    lastmod: '2026-10-19',
    limit: 2,
  });
  assert.equal(large.files[0].name, 'sitemap.xml');
  assert.match(large.files[0].xml, /<sitemapindex/);
  assert.equal(large.files.length, 4);
  assert.deepEqual(large.files.slice(1).map((file: { name: string }) => file.name), ['sitemap-1.xml', 'sitemap-2.xml', 'sitemap-3.xml']);
  assert.match(large.files[1].xml, /<loc>https:\/\/puppetnet-web\.pages\.dev\/entity\/e-0\/<\/loc>/);
  for (const file of large.files) assert.equal(file.xml.includes('&'), false, 'no unescaped ampersand');
});

test('renderUrlset escapes a hostile query string', () => {
  const xml = renderUrlset([{ loc: '/x?a=1&b=<2>', priority: '0.5' }], ORIGIN, '2026-10-19');
  assert.match(xml, /a=1&amp;b=&lt;2&gt;/);
  assert.equal(xml.includes('&b='), false);
});

/* ---------------- Wave 3: structural analytics + ego-graph ---------------- */

test('export analytics are parsed, bounded and never invented', () => {
  const dataset = normalizeEntityDataset({
    nodes: [
      { id: 'a', name: 'A GmbH', type: 'Organization', degree: 3, rank: 0.1234567, community: 'c000', firstSeen: '2026-01-02', lastSeen: '2026-02-03' },
      { id: 'b', name: 'B Ltd', type: 'Organization', degree: -1, rank: 2, community: '<script>', firstSeen: 'gestern' },
      { id: 'c', name: 'C Person', type: 'Person' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS' },
      { source: 'c', target: 'a', type: 'DIRECTOR_OF' },
    ],
  });
  const a = dataset.entities.find((entity) => entity.id === 'a')!;
  assert.deepEqual(a.analytics, { degree: 3, rank: 0.123457, community: 'c000', firstSeen: '2026-01-02', lastSeen: '2026-02-03' });
  const b = dataset.entities.find((entity) => entity.id === 'b')!;
  assert.equal(b.analytics, undefined, 'invalid metrics are dropped entirely, never clamped into looking real');
  const c = dataset.entities.find((entity) => entity.id === 'c')!;
  assert.equal(c.analytics, undefined, 'missing metrics stay missing');
  // neighbours carry the structural metrics for the connections list
  const cToA = c.related.find((item) => item.id === 'a')!;
  assert.equal(cToA.degree, 3);
  assert.equal(cToA.rank, 0.123457);
  assert.equal(cToA.community, 'c000');
  assert.equal(a.related.find((item) => item.id === 'c')!.degree, undefined);
});

test('the demo dataset publishes analytics without inventing timelines', () => {
  const { dataset } = sample();
  const nordlicht = dataset.entities.find((entity) => entity.id === 'org:nordlicht-holdings')!;
  assert.equal(nordlicht.analytics?.degree, 3);
  assert.equal(nordlicht.analytics?.community, 'c000');
  assert.equal(nordlicht.analytics?.firstSeen, '2026-09-12');
  const hamburg = dataset.entities.find((entity) => entity.id === 'loc:hamburg')!;
  assert.equal(hamburg.analytics?.firstSeen, undefined, 'entities without dated evidence keep no invented timeline');
  assert.equal(hamburg.analytics?.lastSeen, undefined);
});

test('JSON-LD carries metrics, sources and person links inside the pinned @graph shape', () => {
  const dataset = sample().dataset;
  const anna = dataset.entities.find((entity) => entity.id === 'per:anna-berg')!;
  const ld = buildEntityJsonLd(anna, ORIGIN);
  assert.equal((ld['@graph'] as unknown[]).length, 3, 'graph stays website + subject + breadcrumb');
  const subject = (ld['@graph'] as Array<Record<string, unknown>>)[1];
  const props = subject.additionalProperty as Array<Record<string, unknown>>;
  assert.ok(props.some((item) => item.name === 'degree' && item.value === anna.analytics!.degree));
  assert.ok(props.some((item) => item.name === 'PageRank'));
  assert.ok(props.some((item) => item.name === 'community' && item.value === 'c000'));
  assert.ok(props.some((item) => item.name === 'firstMention' && item.value === '2026-09-20'));
  const subjectOf = subject.subjectOf as Array<Record<string, unknown>>;
  assert.ok(subjectOf.length > 0 && subjectOf.every((item) => item['@type'] === 'CreativeWork'));
  const knows = subject.knows as Array<Record<string, string>>;
  assert.ok(knows.some((item) => item['@id'].endsWith('#entity') && item['@id'].startsWith(ORIGIN)), 'person neighbours are absolute @id references');
  const org = buildEntityJsonLd(dataset.entities.find((entity) => entity.id === 'org:nordlicht-holdings')!, ORIGIN);
  assert.equal(((org['@graph'] as Array<Record<string, unknown>>)[1]).knows, undefined, 'knows is reserved for Person profiles');
  // entities without analytics keep the old JSON-LD untouched
  const bare = normalizeEntityDataset({ nodes: [{ id: 'n', name: 'Nur Name', type: 'Person' }] }).entities[0];
  const bareSubject = (buildEntityJsonLd(bare, ORIGIN)['@graph'] as Array<Record<string, unknown>>)[1];
  assert.equal(bareSubject.additionalProperty, undefined);
  assert.equal(bareSubject.knows, undefined);
});

test('the ego-graph SVG is deterministic, escaped, bounded and linked', () => {
  const { dataset } = sample();
  const nordlicht = dataset.entities.find((entity) => entity.id === 'org:nordlicht-holdings')!;
  const svg = buildEgoGraphSvg(nordlicht);
  assert.ok(svg && svg.startsWith('<svg') && svg.endsWith('</svg>'));
  assert.equal(svg, buildEgoGraphSvg(nordlicht), 'the same entity renders byte-identical markup');
  assert.match(svg, /role="img"/);
  assert.match(svg, /aria-labelledby="ego-title-nordlicht[^"]* ego-desc-nordlicht[^"]*"/);
  assert.ok(svg.includes('<title id='), 'the SVG carries an accessible title');
  const neighborSlugs = [...new Set(nordlicht.related.map((item) => item.slug))];
  assert.equal((svg.match(/class="ego-node"/g) ?? []).length, neighborSlugs.length, 'one circle per unique neighbour');
  for (const slug of neighborSlugs) assert.ok(svg.includes(entityPath(slug)), `neighbour ${slug} links to its profile`);
  assert.equal(buildEgoGraphSvg(normalizeEntityDataset({ nodes: [{ id: 'z', name: 'Z', type: 'Person' }] }).entities[0]), null, 'no neighbours means no graph');
  // hostile names cannot break out of text or attributes
  const evil = normalizeEntityDataset({
    nodes: [
      { id: 'x', name: '"><script>alert(1)</script>', type: 'Organization' },
      { id: 'y', name: "Y & Co <b> 'quoted'", type: 'Organization' },
    ],
    edges: [{ source: 'x', target: 'y', type: 'OWNS' }],
  }).entities.find((entity) => entity.id === 'x')!;
  const evilSvg = buildEgoGraphSvg(evil)!;
  assert.equal(evilSvg.includes('<script'), false);
  assert.equal(evilSvg.includes('"><script'), false);
  assert.ok(evilSvg.includes('&lt;script&gt;'), 'angle brackets are escaped');
  assert.ok(evilSvg.includes('&amp;'), 'ampersands are escaped');
  // multi-relationship neighbours merge into one circle with all predicates
  const dup = normalizeEntityDataset({
    nodes: [{ id: 'a', name: 'A', type: 'Organization' }, { id: 'b', name: 'B', type: 'Organization' }],
    edges: [{ source: 'a', target: 'b', type: 'OWNS' }, { source: 'a', target: 'b', type: 'DIRECTOR_OF' }],
  }).entities.find((entity) => entity.id === 'a')!;
  const dupSvg = buildEgoGraphSvg(dup)!;
  assert.equal((dupSvg.match(/class="ego-node"/g) ?? []).length, 1);
  assert.equal((dupSvg.match(/<line class="ego-edge /g) ?? []).length, 1);
  assert.ok(dupSvg.includes('DIRECTOR_OF, OWNS'), 'merged predicates are labelled');
  // bidirectional pairs get one solid and one dashed line
  const both = normalizeEntityDataset({
    nodes: [{ id: 'a', name: 'A', type: 'Organization' }, { id: 'b', name: 'B', type: 'Organization' }],
    edges: [{ source: 'a', target: 'b', type: 'OWNS' }, { source: 'b', target: 'a', type: 'OWES' }],
  }).entities.find((entity) => entity.id === 'a')!;
  const bothSvg = buildEgoGraphSvg(both)!;
  assert.equal((bothSvg.match(/class="ego-edge ego-edge-out"/g) ?? []).length, 1);
  assert.equal((bothSvg.match(/class="ego-edge ego-edge-in"/g) ?? []).length, 1);
  assert.ok(bothSvg.includes('stroke-dasharray="5 4"'), 'incoming lines are dashed');
});
