import { test } from 'node:test';
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { normalizeDataset, demoDataset, filterDataset, groupDataset, geographicPoints, evidenceFor, evidenceForEdge, safeUrl, cypherExport, levelFor, shortestPath, fuzzyScore, rankSearch, resolveNodeRef, nodesToCSV, edgesToCSV, communityColor, encodeViewState, decodeViewState, namePartition, brokerShares, egoNetwork, mentionTimeline, duplicateCandidates, monthWindow, bundleEdges, structuralVulnerability, pathTimeline, normalizeNote, readNotes, caseReport, NOTE_LIMIT, EGO_DEPTH_LIMIT, VULNERABILITY_NODE_LIMIT, normalizeInsights, ANALYSIS_LIMITS, TYPES, type Dataset, type Filters } from '../apps/web/app/dashboard/graph-data';
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

test('Louvain partition naming is stable: largest first, ties by smallest member id', () => {
  const named = namePartition({ b: 1, c: 1, a: 2, d: 0 });
  assert.equal(named.get('b'), 'l000');
  assert.equal(named.get('c'), 'l000');
  assert.equal(named.get('a'), 'l001', 'singleton tie breaks by smallest member id');
  assert.equal(named.get('d'), 'l002');
  assert.deepEqual([...namePartition({ d: 0, a: 2, c: 1, b: 1 })], [...named], 'key order does not matter');
  assert.equal(namePartition({}).size, 0);
});

test('broker shares normalize, drop zero/invalid betweenness and stay bounded', () => {
  const brokers = brokerShares({ a: 3, b: 1, c: 0, d: Number.NaN });
  assert.equal(brokers.length, 2);
  assert.equal(brokers[0].id, 'a');
  assert.ok(Math.abs(brokers[0].share - 0.75) < 1e-9);
  assert.ok(Math.abs(brokers[1].share - 0.25) < 1e-9);
  assert.deepEqual(brokerShares({ a: 0, b: 0 }), [], 'all-zero betweenness is an honest empty list');
  const many: Record<string, number> = {};
  for (let i = 0; i < 30; i++) many[`n${i}`] = i + 1;
  assert.equal(brokerShares(many).length, 10, 'top list stays bounded');
  assert.deepEqual(brokerShares({ z: 1, y: 1 }).map(b => b.id), ['y', 'z'], 'value ties break by id');
  assert.ok(ANALYSIS_LIMITS.broker < ANALYSIS_LIMITS.louvain);
});

test('carries, validates and bounds export insights', () => {
  const base = { nodes: [{ id: 'a' }, { id: 'b' }], edges: [{ source: 'a', target: 'b' }] };
  const insight = { rule: 'nominee-chain', version: 1, title: 'Mögliche Strohmann-Kette', description: 'A ist Strohmann für B.', support: .8,
    entities: [{ id: 'a', name: 'A', type: 'Person' }, { id: 'b', name: 'B' }], evidence: ['edge:1'] };
  const data = normalizeDataset({ ...base, insights: [insight] });
  assert.deepEqual(data.insights, [insight]);
  assert.equal(filterDataset(data, filters).insights?.length, 1);
  assert.equal('insights' in normalizeDataset(base), false, 'insight-free exports stay untouched');
  // Anything malformed is dropped, never trusted.
  assert.equal(normalizeDataset({ ...base, insights: 'nope' }).insights, undefined);
  assert.equal(normalizeDataset({ ...base, insights: [{ title: 'ohne rule' }, { rule: '', title: 'x' }, null] }).insights, undefined);
  const direct = normalizeInsights([{ rule: 'hub-spoke', title: 'Stern', support: 5, entities: [{ id: '', name: 'x' }, { id: 'h', name: 'Hub' }], evidence: [1, '', 'e'] }, { rule: 'x' }]);
  assert.equal(direct.length, 1);
  assert.equal(direct[0].version, 1);
  assert.equal(direct[0].support, 1, 'support is clamped to [0,1]');
  assert.deepEqual(direct[0].entities, [{ id: 'h', name: 'Hub' }]);
  assert.deepEqual(direct[0].evidence, ['1', 'e']);
  const capped = normalizeInsights(Array.from({ length: 40 }, (_, i) => ({ rule: 'r', title: `t${i}`, description: 'd', support: .5, entities: [], evidence: [] })));
  assert.equal(capped.length, 24, 'the UI never renders more than the export cap');
});

test('edge evidence resolves publication documents with honest fallbacks', () => {
  const data = normalizeDataset({
    nodes: [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }, { id: 'doc:x', name: 'Referenced Node Doc' }],
    edges: [{ source: 'a', target: 'b', id: 'e1', doc_ids: ['doc:1', 'doc:x', 'doc:missing'] }],
    evidence: [{ id: 'doc:1', title: 'Registerauszug', url: 'https://example.org/1' }],
  });
  const evidence = evidenceForEdge(data, data.edges[0].id);
  assert.equal(evidence.length, 3);
  assert.equal(evidence[0].title, 'Registerauszug');
  assert.equal(evidence[0].url, 'https://example.org/1');
  assert.equal(evidence[1].title, 'Referenced Node Doc', 'a document referenced by node id resolves to its label');
  assert.equal(evidence[2].title, 'doc:missing', 'unknown references stay honest placeholders');
  assert.deepEqual(evidenceForEdge(data, 'no-such-edge'), []);
});

test('wave 7 ego expansion is deterministic, direction-agnostic and depth-capped', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'A' }, { key: 'b', name: 'B' }, { key: 'c', name: 'C' },
      { key: 'd', name: 'D' }, { key: 'x', name: 'X' }],
    edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }, { source: 'c', target: 'd' }],
    evidence: [],
  });
  assert.deepEqual(egoNetwork(data, 'c', 1)?.sort(), ['b', 'c', 'd']);
  assert.deepEqual(egoNetwork(data, 'a', 2), ['a', 'b', 'c'], 'BFS order: center first');
  assert.deepEqual(egoNetwork(data, 'a', 3), ['a', 'b', 'c', 'd']);
  assert.deepEqual(egoNetwork(data, 'a', 99), egoNetwork(data, 'a', EGO_DEPTH_LIMIT), 'depth clamps at the limit');
  assert.deepEqual(egoNetwork(data, 'a', 0), egoNetwork(data, 'a', 1), 'junk depth falls back to one hop');
  assert.deepEqual(egoNetwork(data, 'd', 1), ['d', 'c'], 'edge direction is ignored');
  assert.deepEqual(egoNetwork(data, 'x', 2), ['x'], 'isolated node: honest self-only neighborhood');
  assert.equal(egoNetwork(data, 'missing', 1), null, 'unknown center is null, never fabricated');
  assert.deepEqual(egoNetwork(data, 'a', 2), egoNetwork(data, 'a', 2), 'same input, same neighborhood');
  // Ego runs on the filtered slice: dangling edges to filtered-out nodes never leak in.
  const slice = filterDataset(data, { confidence: 0, weight: 0, types: ['Person', 'Organization', 'Location', 'Craft', 'Document', 'Other'], lod: true, ratio: 1 });
  assert.deepEqual(egoNetwork(slice, 'b', 1)?.sort(), ['a', 'b', 'c']);
});

test('wave 7 mention timeline buckets first mentions, drops undated and bounds the window', () => {
  const nodes = normalizeDataset({
    nodes: [
      { key: 'n1', name: 'N1', firstSeen: '2026-01-15' },
      { key: 'n2', name: 'N2', firstSeen: '2026-01-20T10:00:00Z' },
      { key: 'n3', name: 'N3', firstSeen: '2026-03-01' },
      { key: 'n4', name: 'N4' },
      { key: 'n5', name: 'N5', firstSeen: '15.01.2026' },
    ],
    edges: [], evidence: [],
  }).nodes;
  assert.deepEqual(mentionTimeline(nodes), [
    { month: '2026-01', count: 2 },
    { month: '2026-03', count: 1 },
  ], 'undated and non-ISO entities are honestly absent');
  assert.deepEqual(mentionTimeline([]), [], 'empty slice stays empty');
  const wide = Array.from({ length: 30 }, (_, i) => ({
    key: `w${i}`, name: `W${i}`, firstSeen: `202${4 + Math.floor(i / 12)}-${String((i % 12) + 1).padStart(2, '0')}-05`,
  }));
  const bounded = mentionTimeline(normalizeDataset({ nodes: wide, edges: [], evidence: [] }).nodes, 12);
  assert.equal(bounded.length, 12, 'the window is bounded');
  assert.ok(bounded.every((bucket, i) => i === 0 || bucket.month > bounded[i - 1].month), 'ascending, most recent kept');
});

test('wave 8 duplicate hints fold labels conservatively and never judge', () => {
  const data = normalizeDataset({
    nodes: [
      { key: 'p1', name: 'Anna Meier', entity_type: 'person' },
      { key: 'p2', name: 'anna   meier', entity_type: 'person' },
      { key: 'p3', name: 'Anna Meier', entity_type: 'person' },
      { key: 'o1', name: 'Nordlicht GmbH', entity_type: 'org' },
      { key: 'o2', name: 'Nordlicht GmbH', entity_type: 'person' },
      { key: 'u1', name: 'Einmalig', entity_type: 'org' },
      { key: 'm1', name: 'Anna Meier', entity_type: 'person' },
    ],
    edges: [], evidence: [],
  });
  const hints = duplicateCandidates(data.nodes);
  assert.equal(hints.length, 2, 'only real label collisions are hints');
  assert.deepEqual(hints[0].ids, ['p1', 'p2', 'p3', 'm1'], 'largest group first');
  assert.equal(hints[0].sameType, true);
  assert.deepEqual(hints[1].ids, ['o1', 'o2']);
  assert.equal(hints[1].sameType, false, 'type mismatch is surfaced, not hidden');
  assert.equal(hints[1].label, 'Nordlicht GmbH');
  const grouped = groupDataset(data, 1).nodes.filter(n => n.group);
  assert.deepEqual(duplicateCandidates(grouped), [], 'LoD display groups are not entities');
  assert.deepEqual(duplicateCandidates(data.nodes), hints, 'deterministic');
  assert.equal(duplicateCandidates(data.nodes, 1).length, 1, 'bounded');
});

test('wave 8 month windows come from the real calendar or not at all', () => {
  assert.deepEqual(monthWindow('2026-02'), { from: '2026-02-01', to: '2026-02-28' });
  assert.deepEqual(monthWindow('2024-02'), { from: '2024-02-01', to: '2024-02-29' }, 'leap years are real');
  assert.deepEqual(monthWindow('2026-12'), { from: '2026-12-01', to: '2026-12-31' });
  assert.equal(monthWindow('2026-13'), null);
  assert.equal(monthWindow('2026-00'), null);
  assert.equal(monthWindow('2026-1'), null);
  assert.equal(monthWindow(''), null);
});

test('wave 9 edge bundles keep the strongest evidence and the full type story', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'A' }, { key: 'b', name: 'B' }, { key: 'c', name: 'C' }, { key: 'd', name: 'D' }],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', confidence: 0.6, weight: 1, evidenceIds: ['e1'] },
      { source: 'b', target: 'a', type: 'ACQUIRED', confidence: 0.9, weight: 1, evidenceIds: ['e2'] },
      { source: 'a', target: 'c', type: 'MET_WITH', confidence: 0.5, weight: 1, evidenceIds: ['e3'] },
      { source: 'd', target: 'd', type: 'SELF', confidence: 0.4, weight: 1, evidenceIds: ['e4'] },
    ],
    evidence: [],
  });
  const bundled = bundleEdges(data.edges);
  assert.equal(bundled.length, 3, 'parallel pair merges, self-loops stay separate');
  const pair = bundled[0];
  assert.equal(pair.source, 'a');
  assert.equal(pair.target, 'b', 'endpoints are normalized for stable rendering');
  assert.equal(pair.count, 2);
  assert.deepEqual(pair.types, ['ACQUIRED', 'OWNS'], 'every relationship type stays visible');
  assert.equal(pair.type, 'ACQUIRED', 'the strongest documented relationship names the bundle');
  assert.equal(pair.confidence, 0.9);
  assert.deepEqual(pair.evidenceIds, ['e1', 'e2'], 'provenance unions instead of dropping');
  assert.equal(pair.weight, 2, 'weights add up');
  assert.deepEqual(bundleEdges(data.edges), bundled, 'deterministic');
  assert.equal(data.edges.length, 4, 'the input is never mutated');
  assert.deepEqual(bundleEdges([]), []);
});

test('wave 9 vulnerability: bridges, articulation points and the k-core nucleus', () => {
  const build = (pairs: [string, string][], count = 3) => normalizeDataset({
    nodes: Array.from({ length: count }, (_, i) => ({ key: String.fromCharCode(97 + i), name: String.fromCharCode(65 + i) })),
    edges: pairs.map(([source, target]) => ({ source, target })), evidence: [],
  });
  const path = build([['a', 'b'], ['b', 'c'], ['c', 'd']], 4);
  assert.deepEqual(structuralVulnerability(path.nodes, path.edges)?.cutVertices.map(v => v.id), ['b', 'c']);
  assert.equal(structuralVulnerability(path.nodes, path.edges)?.cutEdges, 3);
  assert.deepEqual([structuralVulnerability(path.nodes, path.edges)?.maxCore, structuralVulnerability(path.nodes, path.edges)?.maxCoreSize], [1, 4], 'a path is one 1-core');
  const tail = build([['a', 'b'], ['b', 'c'], ['c', 'a'], ['c', 'd'], ['d', 'e']], 5);
  assert.deepEqual(structuralVulnerability(tail.nodes, tail.edges)?.cutVertices.map(v => v.id), ['c', 'd'], 'the triangle is cohesive, the tail is fragile');
  assert.equal(structuralVulnerability(tail.nodes, tail.edges)?.cutEdges, 2);
  assert.deepEqual([structuralVulnerability(tail.nodes, tail.edges)?.maxCore, structuralVulnerability(tail.nodes, tail.edges)?.maxCoreSize], [2, 3]);
  const cycle = build([['a', 'b'], ['b', 'c'], ['c', 'a']]);
  assert.deepEqual(structuralVulnerability(cycle.nodes, cycle.edges)?.cutVertices, [], 'a cycle has no single point of failure');
  assert.equal(structuralVulnerability(cycle.nodes, cycle.edges)?.cutEdges, 0);
  const parallel = build([['a', 'b'], ['b', 'a'], ['b', 'c']], 3);
  assert.deepEqual(structuralVulnerability(parallel.nodes, parallel.edges)?.cutVertices.map(v => v.id), ['b'], 'parallel edges count as one relationship');
  assert.equal(structuralVulnerability(parallel.nodes, parallel.edges)?.cutEdges, 2);
  const split = build([['a', 'b'], ['c', 'd']], 4);
  assert.deepEqual(structuralVulnerability(split.nodes, split.edges)?.cutVertices, [], 'disconnected parts have no cut vertices here');
  assert.equal(structuralVulnerability(split.nodes, split.edges)?.cutEdges, 2);
  assert.equal(structuralVulnerability(path.nodes, path.edges, 12, 2), null, 'above the node limit the answer is null, not a guess');
  assert.deepEqual(structuralVulnerability([], []), { cutVertices: [], cutEdges: 0, maxCore: 0, maxCoreSize: 0 });
  const withGroup = [...path.nodes, { ...path.nodes[0], id: 'group:x', group: true }];
  assert.deepEqual(structuralVulnerability(withGroup, path.edges)?.cutVertices.map(v => v.id), ['b', 'c'], 'LoD display groups are not entities');
  const bounded = structuralVulnerability(tail.nodes, tail.edges, 1);
  assert.equal(bounded?.cutVertices.length, 1, 'the list is bounded');
  assert.equal(bounded?.cutVertices[0].degree, 3, 'highest degree first');
});

test('wave 10 path timelines date every hop and flag anachronisms honestly', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'A' }, { key: 'b', name: 'B' }, { key: 'c', name: 'C' }, { key: 'd', name: 'D' }],
    evidence: [
      { id: 'd1', title: 'Doc 1', date: '2020-01-01T08:00:00Z' },
      { id: 'd2', title: 'Doc 2', date: '2021-06-01' },
      { id: 'd3a', title: 'Doc 3a', date: '2019-05-01' },
      { id: 'd3b', title: 'Doc 3b', date: '2019-07-15' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['d1'] },
      { source: 'b', target: 'c', type: 'ACQUIRED', evidenceIds: ['d2'] },
      { source: 'c', target: 'd', type: 'MET_WITH', evidenceIds: ['d3a', 'd3b'] },
    ],
  });
  const timeline = pathTimeline(data, ['a', 'b', 'c', 'd']);
  assert.equal(timeline?.hops.length, 3);
  assert.deepEqual([timeline?.hops[0].first, timeline?.hops[0].last], ['2020-01-01', '2020-01-01'], 'timestamps normalize to the day');
  assert.equal(timeline?.hops[2].evidence, 2, 'provenance unions per hop');
  assert.deepEqual([timeline?.hops[2].first, timeline?.hops[2].last], ['2019-05-01', '2019-07-15'], 'the window spans all documents');
  assert.equal(timeline?.hops[2].ordered, false, 'a hop documented before the previous one is an anachronism');
  assert.equal(timeline?.anachronisms, 1);
  assert.deepEqual([timeline?.first, timeline?.last], ['2019-05-01', '2021-06-01'], 'the chain span is the outer envelope');
  assert.equal(pathTimeline(data, ['a']), null, 'a single node is not a chain');
  assert.equal(pathTimeline(data, ['a', 'd']), null, 'a hop without a documented relation is null, not invented');
  assert.deepEqual(pathTimeline(data, ['a', 'b', 'c', 'd']), timeline, 'deterministic');
  const undated = normalizeDataset({
    nodes: [{ key: 'a', name: 'A' }, { key: 'b', name: 'B' }],
    evidence: [], edges: [{ source: 'a', target: 'b', type: 'OWNS' }],
  });
  const bare = pathTimeline(undated, ['a', 'b']);
  assert.equal(bare?.hops[0].first, undefined, 'undated hops stay honestly empty');
  assert.equal(bare?.anachronisms, 0, 'missing dates are never turned into a verdict');
});

test('wave 10 failure simulation counts the parts left behind', () => {
  const data = normalizeDataset({
    nodes: ['a', 'b', 'c', 'd', 'e'].map(key => ({ key, name: key.toUpperCase() })), evidence: [],
    edges: [['a', 'b'], ['b', 'c'], ['c', 'a'], ['c', 'd'], ['d', 'e']].map(([source, target]) => ({ source, target })),
  });
  const vulnerability = structuralVulnerability(data.nodes, data.edges);
  const c = vulnerability?.cutVertices.find(vertex => vertex.id === 'c');
  const d = vulnerability?.cutVertices.find(vertex => vertex.id === 'd');
  assert.deepEqual(c?.impact, { components: 2, largest: 2 }, 'removing C leaves the triangle remnant and the tail');
  assert.deepEqual(d?.impact, { components: 2, largest: 3 }, 'removing D isolates the triangle');
  assert.ok(vulnerability!.cutVertices.every(vertex => vertex.impact.components >= 2), 'an articulation point always splits something');
  const path = normalizeDataset({
    nodes: ['a', 'b', 'c'].map(key => ({ key, name: key.toUpperCase() })), evidence: [],
    edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }],
  });
  assert.deepEqual(structuralVulnerability(path.nodes, path.edges)?.cutVertices.find(v => v.id === 'b')?.impact, { components: 2, largest: 1 });
});

test('wave 11 research notes normalize, cap and drop junk instead of trusting storage', () => {
  assert.equal(normalizeNote('  line1\r\nline2  '), 'line1\nline2', 'newlines normalize, edges trim');
  assert.equal(normalizeNote('x'.repeat(2500)).length, NOTE_LIMIT, 'notes are capped');
  assert.equal(normalizeNote(42), '', 'non-strings never become notes');
  assert.equal(normalizeNote(null), '');
  assert.deepEqual(readNotes(JSON.stringify({ a: '  keep  ', b: '', c: 7 })), { a: 'keep' }, 'empty and non-string entries drop out');
  assert.deepEqual(readNotes('not json'), {}, 'broken storage is empty, not fatal');
  assert.deepEqual(readNotes(null), {});
  const many = Object.fromEntries(Array.from({ length: 600 }, (_, i) => [`n${i}`, `note ${i}`]));
  assert.equal(Object.keys(readNotes(JSON.stringify(many))).length, 500, 'the store stays bounded');
  assert.equal(readNotes({ a: 'x'.repeat(5000) }).a.length, NOTE_LIMIT, 'stored notes are re-capped on read');
});

test('wave 11 case report is deterministic, bilingual and keeps hints and proof apart', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'z', name: 'Zeta GmbH' }, { key: 'm', name: 'Alpha Meier' }],
    edges: [{ source: 'm', target: 'z', type: 'OWNS' }], evidence: [],
    analytics: { communities: 1, density: 0.5, hubs: [{ id: 'z', name: 'Zeta GmbH', rank: 0.42, degree: 3 }] },
  });
  const stats = { totalNodes: 2, totalEdges: 1, filteredNodes: 2, filteredEdges: 1, locatedNodes: 0, grouped: false, level: 'Einzelknoten', focused: false, truncated: false };
  const input = {
    stats, nodes: data.nodes, edges: data.edges, analytics: data.analytics,
    timeline: [{ month: '2026-01', count: 4 }],
    duplicates: [{ label: 'Zeta GmbH', ids: ['z', 'z2'], sameType: true }],
    vulnerability: { cutVertices: [{ id: 'm', label: 'Alpha Meier', degree: 1, impact: { components: 2, largest: 1 } }], cutEdges: 1, maxCore: 1, maxCoreSize: 2 },
    notes: { z: 'Geschäftsführerin prüfen\nzweite Quelle fehlt', m: '   ' },
  };
  const report = caseReport(input, '2026-10-20T12:00:00.000Z');
  assert.ok(report.startsWith('# PuppetNET — Fallbericht'), 'German by default');
  assert.ok(report.includes('Erzeugt: 2026-10-20T12:00:00.000Z'), 'timestamps are passed in, not invented');
  assert.ok(report.includes('## Ermittlungsnotizen (1)'), 'empty notes never inflate the count');
  assert.ok(report.includes('- **Zeta GmbH** (`z`)\n  Geschäftsführerin prüfen\n  zweite Quelle fehlt'), 'multi-line notes keep their structure');
  assert.ok(report.includes('zerfällt in 2 Teile, größter 1'), 'the failure simulation travels into the report');
  assert.ok(report.includes('Hinweise, keine Beweise'), 'the closing line keeps hints and proof apart');
  assert.equal(caseReport(input, '2026-10-20T12:00:00.000Z'), report, 'deterministic');
  const english = caseReport(input, '2026-10-20T12:00:00.000Z', 'en');
  assert.ok(english.startsWith('# PuppetNET — case report'));
  assert.ok(english.includes('View level: single nodes'), 'the view level is translated, not leaked');
  assert.ok(english.includes('Hints, not proof'), 'the disclaimer travels');
  assert.equal(caseReport({ stats, nodes: data.nodes, edges: data.edges }, '2026-10-20'), caseReport({ stats, nodes: data.nodes, edges: data.edges }, '2026-10-20'), 'sparse input stays deterministic');
});
