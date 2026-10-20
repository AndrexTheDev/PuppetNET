import { test } from 'node:test';
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { normalizeDataset, demoDataset, filterDataset, groupDataset, geographicPoints, evidenceFor, safeUrl, cypherExport, levelFor, shortestPath, fuzzyScore, rankSearch, resolveNodeRef, nodesToCSV, edgesToCSV, communityColor, encodeViewState, decodeViewState, TYPES, type Dataset, type Filters } from '../apps/web/app/dashboard/graph-data';
import { apiBase, readResponse } from '../apps/web/app/dashboard/api';
const filters: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: true, ratio: 1 };
test('normalizes Worker and Graphology nodes without inventing coordinates', () => {
  const data = normalizeDataset({ nodes: [
    { key: 'a', name: 'Alice', entity_type: 'PER', confidence: .8, props: { latitude: '0', longitude: '0' } },
    { key: 'b', attributes: { label: 'Example', type: 'Company' } },
    { id: 'c', type: 'Location', lat: 91, lon: 30 },
  ], edges: [{ source: 'a', target: 'b', weight: 4, confidence: -.1 }] });
  assert.equal(data.nodes[0].type, 'Person');
  assert.equal(data.nodes[0].lat, 0);
  assert.equal(data.nodes[1].type, 'Organization');
  assert.equal(data.nodes[1].lat, undefined);
  assert.equal(data.nodes[2].lat, undefined);
  assert.equal(data.edges[0].weight, 1);
  assert.equal(data.edges[0].confidence, 0);
});
test('rejects malformed, dangling and duplicate graph IDs', () => {
  assert.throws(() => normalizeDataset({}), /nodes/);
  assert.throws(() => normalizeDataset({ nodes: [{ id: 'a' }, { id: 'a' }], edges: [] }), /doppelte/);
  assert.throws(() => normalizeDataset({ nodes: [{ id: 'a' }], edges: [{ source: 'a', target: 'b' }] }), /fehlende Knoten/);
});
test('filters apply node types and both edge scores, preserving graph closure', () => {
  const data = demoDataset(100);
  const result = filterDataset(data, { ...filters, confidence: .6, weight: .8, types: ['Person', 'Organization'] });
  const ids = new Set(result.nodes.map(n => n.id));
  assert.ok(result.nodes.length > 0 && result.nodes.length < 100);
  assert.ok(result.nodes.every(n => n.confidence >= .6 && ['Person', 'Organization'].includes(n.type)));
  assert.ok(result.edges.every(e => e.weight >= .8 && e.confidence >= .6 && ids.has(e.source) && ids.has(e.target)));
  assert.equal(filterDataset(data, { ...filters, types: [] }).nodes.length, 0);
  assert.equal(data.nodes.length, 100);
});
test('LoD activates strictly above 5000 and adapts to camera zoom', () => {
  assert.equal(levelFor(5000, filters), 'Einzelknoten');
  assert.equal(levelFor(5001, filters), 'Übersicht');
  assert.equal(levelFor(5001, { ...filters, ratio: .3 }), 'Gruppen');
  assert.equal(levelFor(5001, { ...filters, ratio: .1 }), 'Feingruppen');
  assert.equal(levelFor(5001, { ...filters, lod: false }), 'Einzelknoten');
});
test('120000-element stress graph conserves ALL nodes and edges under LoD', () => {
  const start = performance.now();
  const data = demoDataset(30_000), grouped = groupDataset(data, filters);
  assert.equal(data.nodes.length + data.edges.length, 120_000);
  assert.ok(grouped.nodes.length < 5000);
  assert.equal(grouped.nodes.reduce((n, g) => n + g.count!, 0), data.nodes.length);
  assert.equal(grouped.edges.reduce((n, e) => n + e.count!, 0) + grouped.nodes.reduce((n, g) => n + g.internalEdges!, 0), data.edges.length);
  assert.equal(grouped.members.size, data.nodes.length);
  assert.ok(groupDataset(data, { ...filters, ratio: .1 }).nodes.length > grouped.nodes.length);
  assert.equal(groupDataset(data, { ...filters, lod: false }).nodes.length, 30_000);
  console.log(`120000-element fixture + LoD: ${(performance.now() - start).toFixed(0)} ms (not a GPU FPS benchmark)`);
});
test('geo grouping bounds markers without losing located nodes or accepting null as zero', () => {
  const data = normalizeDataset({ nodes: [{ id: 'a', lat: null, lon: null }, { id: 'b', lat: '', lon: '' }, { id: 'c', lat: 0, lon: 0 }], edges: [] });
  assert.equal(geographicPoints(data.nodes).count, 1);
  const large = demoDataset(30_000);
  const geo = geographicPoints(large.nodes);
  assert.ok(geo.points.length <= 1000);
  assert.equal(geo.points.reduce((n, p) => n + p.count, 0), geo.count);
});
test('EVIDENCED_BY, MENTIONS and document IDs resolve to safe citations', () => {
  const data = normalizeDataset({ nodes: [
    { id: 'a', type: 'Person', doc_ids: ['d2'] },
    { id: 'd', type: 'Document', title: '<script>unsafe title</script>', url: 'javascript:alert(1)' },
    { id: 'd2', type: 'Document', title: 'Source two', url: 'https://example.com/report' },
  ], edges: [{ source: 'a', target: 'd', type: 'EVIDENCED_BY' }, { source: 'd2', target: 'a', type: 'MENTIONS' }] });
  const docs = evidenceFor(data, 'a');
  assert.equal(docs.length, 2);
  assert.equal(docs.find(d => d.id === 'd')?.url, undefined);
  assert.equal(docs.find(d => d.id === 'd2')?.url, 'https://example.com/report');
});
test('Cypher keeps hostile keys exclusively in parameters', () => {
  const key = 'a"}) DETACH DELETE n //';
  const result = cypherExport(key);
  assert.ok(!result.query.includes(key));
  assert.equal(result.parameters.key, key);
  assert.match(result.query, /EVIDENCED_BY/);
  assert.equal(safeUrl('javascript:alert(1)'), undefined);
});
test('API URL validation excludes credential-bearing and insecure remote URLs', () => {
  assert.equal(apiBase(''), '');
  assert.equal(apiBase('https://example.com/'), 'https://example.com');
  for (const url of ['http://example.com', 'https://a:b@example.com', 'https://example.com/?token=abc', 'javascript:alert(1)']) assert.throws(() => apiBase(url));
});
test('bounded API reader rejects non-OK replies and decodes streamed UTF-8', async () => {
  assert.equal(await readResponse(new Response('Grüße')), 'Grüße');
  await assert.rejects(readResponse(new Response('no', { status: 401 })), /401/);
});

/* ------------------------------------------------------------------------- */
/* Wave 2: analysis primitives (pathfinder, fuzzy search, export, permalinks) */
/* ------------------------------------------------------------------------- */

function chainDataset(): Dataset {
  return normalizeDataset({
    nodes: [{ key: 'a', name: 'Anna', entity_type: 'person' }, { key: 'b', name: 'Beta GmbH', entity_type: 'org' },
      { key: 'c', name: 'Centaur AG', entity_type: 'org' }, { key: 'd', name: 'Davos', entity_type: 'loc' }],
    edges: [{ source: 'a', target: 'b', type: 'FOUNDED' }, { source: 'b', target: 'c', type: 'OWNS' },
      { source: 'c', target: 'd', type: 'ASSOCIATED_WITH' }],
    evidence: [],
  });
}

test('shortestPath is deterministic, undirected, hop-bounded and honest about absence', () => {
  const data = chainDataset();
  const path = shortestPath(data, 'a', 'd');
  assert.deepEqual(path?.nodes, ['a', 'b', 'c', 'd']);
  assert.equal(path?.edges.length, 3);
  assert.deepEqual(shortestPath(data, 'd', 'a')?.nodes, ['d', 'c', 'b', 'a']);
  assert.equal(shortestPath(data, 'a', 'd', 2), null, 'hop bound must refuse longer chains');
  assert.equal(shortestPath(data, 'a', 'missing'), null);
  assert.deepEqual(shortestPath(data, 'a', 'a')?.nodes, ['a']);
  assert.deepEqual(shortestPath(data, 'a', 'd'), shortestPath(data, 'a', 'd'), 'same input, same path');
  // Two equal-length routes: sorted adjacency picks the lexicographically smallest peer.
  const diamond = normalizeDataset({
    nodes: [{ key: 's', name: 'S' }, { key: 'x', name: 'X' }, { key: 'y', name: 'Y' }, { key: 't', name: 'T' }],
    edges: [{ source: 's', target: 'y', type: 'OWNS' }, { source: 's', target: 'x', type: 'OWNS' },
      { source: 'x', target: 't', type: 'OWNS' }, { source: 'y', target: 't', type: 'OWNS' }],
    evidence: [],
  });
  assert.deepEqual(shortestPath(diamond, 's', 't')?.nodes, ['s', 'x', 't']);
});

test('fuzzy search ranks prefixes above substrings above subsequences and stays stable', () => {
  assert.ok(fuzzyScore('anna', 'Anna Meier') > fuzzyScore('anna', 'Banana Anna'));
  assert.ok(fuzzyScore('anna', 'Banana Anna') > fuzzyScore('am', 'Anna Meier'));
  assert.equal(fuzzyScore('xyz', 'Anna Meier'), 0);
  const nodes = normalizeDataset({
    nodes: [{ key: '1', name: 'Anna Meier', entity_type: 'person' }, { key: '2', name: 'Banana', entity_type: 'org' },
      { key: '3', name: 'Ananas', entity_type: 'org' }],
    edges: [], evidence: [],
  }).nodes;
  const ranked = rankSearch(nodes, 'anna');
  assert.equal(ranked.length, 3);
  assert.equal(ranked[0].label, 'Anna Meier', 'prefix match wins');
  assert.equal(ranked[1].label, 'Ananas', 'score ties break deterministically by label');
  assert.deepEqual(rankSearch(nodes, ''), nodes, 'empty query preserves view order');
  assert.equal(resolveNodeRef(nodes, 'Anna Meier')?.id, '1');
  assert.equal(resolveNodeRef(nodes, 'ananas')?.id, '3', 'fuzzy fallback resolves');
  assert.equal(resolveNodeRef(nodes, ''), null);
  assert.equal(resolveNodeRef(nodes, 'zzz'), null);
});

test('CSV export is RFC-4180 escaped and column-stable', () => {
  const data = normalizeDataset({ nodes: [{ key: 'a"1', name: 'Meier, Anna', entity_type: 'person' }], edges: [], evidence: [] });
  const csv = nodesToCSV(data.nodes);
  assert.match(csv, /^id,label,type,confidence,degree,rank,community,firstSeen,lastSeen\r\n/);
  assert.match(csv, /"a""1","Meier, Anna",Person/);
  const edges = normalizeDataset({
    nodes: [{ key: 'a', name: 'A' }, { key: 'b', name: 'B' }],
    edges: [{ source: 'a', target: 'b', type: 'OWNS' }], evidence: [],
  }).edges;
  assert.match(edgesToCSV(edges), /^id,source,target,type,confidence,weight\r\n/);
});

test('Wave-1 analytics pass through validated; malformed or oversized blocks are bounded', () => {
  const ok = normalizeDataset({ nodes: [], edges: [], evidence: [],
    analytics: { version: 1, nodes: 2, edges: 1, communities: 1, largestCommunity: 2, density: 0.5,
      hubs: [{ id: 'a', name: 'A', rank: 0.5, degree: 1 }], temporalRange: { first: '2026-01-01', last: '2026-02-01T10:00:00Z' } } });
  assert.equal(ok.analytics?.hubs.length, 1);
  assert.deepEqual(ok.analytics?.temporalRange, { first: '2026-01-01', last: '2026-02-01' });
  assert.equal(normalizeDataset({ nodes: [], edges: [], evidence: [], analytics: ' hostile ' }).analytics, undefined);
  const many = normalizeDataset({ nodes: [], edges: [], evidence: [],
    analytics: { hubs: Array.from({ length: 40 }, (_, i) => ({ id: `h${i}`, name: `H${i}`, rank: 1, degree: 1 })) } });
  assert.equal(many.analytics?.hubs.length, 10, 'hub list stays bounded');
  const node = normalizeDataset({ nodes: [{ key: 'a', name: 'A', entity_type: 'person', degree: 3, rank: 0.25,
    community: 'c000', firstSeen: '2026-01-05T00:00:00Z', lastSeen: 'garbage' }], edges: [], evidence: [] }).nodes[0];
  assert.equal(node.degree, 3);
  assert.equal(node.rank, 0.25);
  assert.equal(node.community, 'c000');
  assert.equal(node.firstSeen, '2026-01-05');
  assert.equal(node.lastSeen, undefined, 'non-ISO dates never pass');
});

test('community and time-window filters keep the graph closed and honest', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'A', entity_type: 'person', community: 'c000', firstSeen: '2026-01-01', lastSeen: '2026-01-10' },
      { key: 'b', name: 'B', entity_type: 'person', community: 'c001', firstSeen: '2026-05-01', lastSeen: '2026-06-01' },
      { key: 'c', name: 'C', entity_type: 'person' }],
    edges: [{ source: 'a', target: 'b', type: 'OWNS' }, { source: 'a', target: 'c', type: 'OWNS' }],
    evidence: [],
  });
  const base: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: true, ratio: 1 };
  assert.deepEqual(filterDataset(data, { ...base, community: 'c000' }).nodes.map(n => n.id), ['a']);
  assert.equal(filterDataset(data, { ...base, community: 'c000' }).edges.length, 0, 'no dangling edges');
  assert.deepEqual(filterDataset(data, { ...base, from: '2026-04-01' }).nodes.map(n => n.id), ['b'], 'undated nodes drop out');
  assert.deepEqual(filterDataset(data, { ...base, from: '2026-01-05', to: '2026-05-05' }).nodes.map(n => n.id), ['a', 'b'], 'overlap semantics');
  assert.equal(filterDataset(data, base).nodes.length, 3, 'absent filters are a no-op');
});

test('permalinks round-trip and reject junk instead of trusting the URL', () => {
  const state = { mode: 'network' as const, q: 'anna', focus: 'person:a', community: 'c000', from: '2026-01-01', to: '2026-02-01', color: 'community' as const };
  assert.deepEqual(decodeViewState(`?${encodeViewState(state)}`), state);
  assert.deepEqual(decodeViewState(`?mode=hack&q=${'x'.repeat(500)}&from=evil&color=rainbow&focus=${'y'.repeat(300)}`), {});
  assert.deepEqual(decodeViewState(''), {});
});

test('community colors are deterministic HSL tokens', () => {
  assert.equal(communityColor('c000'), communityColor('c000'));
  assert.match(communityColor('c001'), /^hsl\(\d{1,3} 58% 58%\)$/);
});
