import { test } from 'node:test';
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { normalizeDataset, demoDataset, filterDataset, groupDataset, geographicPoints, evidenceFor, evidenceForEdge, safeUrl, cypherExport, levelFor, shortestPath, fuzzyScore, rankSearch, resolveNodeRef, nodesToCSV, edgesToCSV, communityColor, encodeViewState, decodeViewState, namePartition, brokerShares, egoNetwork, mentionTimeline, duplicateCandidates, monthWindow, bundleEdges, structuralVulnerability, pathTimeline, normalizeNote, readNotes, caseReport, nextPlaybackMonth, evidenceWindow, compareDuplicates, multiFailureImpact, normalizeSavedName, readSavedViews, upsertSavedView, removeSavedView, evidenceToCSV, parseCaseNotes, mergeNotes, searchNoteIndex, caseBundle, readCaseBundle, provenanceGaps, edgeSources, sharedSourceGroups, normalizeEvidence, edgeSourceStatus, sourceRegistry, pairConflicts, evidenceFreshness, monthsBefore, FRESH_WINDOWS, DEFAULT_FRESH_MONTHS, isFreshWindow, filterDataset, nodeBacking, evidenceRisk, riskToCSV, diffDatasets, diffToCSV, searchCase, noteSnippet, TYPES, SAVED_VIEW_LIMIT, NOTE_LIMIT, EGO_DEPTH_LIMIT, VULNERABILITY_NODE_LIMIT, normalizeInsights, ANALYSIS_LIMITS, TYPES, type Dataset, type Filters } from '../apps/web/app/dashboard/graph-data';
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

test('wave 12 cumulative playback steps through frozen months without looping', () => {
  const months = ['2025-11', '2025-12', '2026-01'];
  assert.equal(nextPlaybackMonth(months, undefined, 1), '2025-11', 'playback starts at the oldest month');
  assert.equal(nextPlaybackMonth(months, '2025-11', 1), '2025-12');
  assert.equal(nextPlaybackMonth(months, '2025-12', 1), '2026-01');
  assert.equal(nextPlaybackMonth(months, '2026-01', 1), null, 'the end is an honest stop, never a wrap-around');
  assert.equal(nextPlaybackMonth(months, '2025-12', -1), '2025-11');
  assert.equal(nextPlaybackMonth(months, '2025-11', -1), null, 'backwards stops before the first month');
  assert.equal(nextPlaybackMonth(months, '1999-01', 1), '2025-11', 'a foreign month restarts from the beginning');
  assert.equal(nextPlaybackMonth([], '2026-01', 1), null);
});

test('wave 12 relationship windows normalize dates and count only what is dated', () => {
  const window = evidenceWindow([
    { id: 'a', title: 'A', date: '2021-03-02T10:00:00Z' },
    { id: 'b', title: 'B', date: 'kein Datum' },
    { id: 'c', title: 'C', date: '2020-12-31' },
  ]);
  assert.deepEqual(window, { first: '2020-12-31', last: '2021-03-02', dated: 2 }, 'undated documents do not widen the window');
  assert.deepEqual(evidenceWindow([]), { first: undefined, last: undefined, dated: 0 });
  assert.deepEqual(evidenceWindow([{ id: 'z', title: 'Z' }]), { first: undefined, last: undefined, dated: 0 });
});

test('wave 13 duplicate comparison weighs shared and own neighbours, never merges', () => {
  const data = normalizeDataset({
    nodes: [
      { key: 'a', name: 'Alpha', entity_type: 'person', community: 'c000', firstSeen: '2020-01-01' },
      { key: 'a2', name: 'Alpha', entity_type: 'person', community: 'c001' },
      { key: 'n1', name: 'Nordlicht GmbH', entity_type: 'org' },
      { key: 'n2', name: 'Hansa AG', entity_type: 'org' },
      { key: 'n3', name: 'Ost GmbH', entity_type: 'org' },
    ],
    edges: [{ source: 'a', target: 'n1' }, { source: 'a2', target: 'n1' }, { source: 'a', target: 'n2' }, { source: 'a2', target: 'n3' }],
    evidence: [],
  });
  const comparison = compareDuplicates(data, ['a', 'a2', 'missing']);
  assert.equal(comparison?.profiles.length, 2, 'unknown ids never fabricate a column');
  assert.deepEqual(comparison?.shared.map(peer => peer.id), ['n1'], 'the shared neighbour is the merge signal');
  assert.deepEqual(comparison?.unique.a, ['Hansa AG']);
  assert.deepEqual(comparison?.unique.a2, ['Ost GmbH']);
  assert.ok(comparison!.profiles.every(profile => profile.neighbors.every(peer => peer.id !== 'a' && peer.id !== 'a2')), 'a duplicate is not its own neighbour');
  assert.deepEqual(comparison?.conflicts, ['community', 'firstSeen'], 'only real differences are flagged');
  assert.equal(compareDuplicates(data, ['a']), null, 'one column is not a comparison');
  assert.deepEqual(compareDuplicates(data, ['a', 'a2']), comparison, 'deterministic');
});

test('wave 13 multi-node failure simulation counts parts, largest survivor and isolation', () => {
  const build = (pairs: [string, string][], count: number) => normalizeDataset({
    nodes: Array.from({ length: count }, (_, i) => ({ key: String.fromCharCode(97 + i), name: String.fromCharCode(65 + i) })),
    edges: pairs.map(([source, target]) => ({ source, target })), evidence: [],
  });
  const path = build([['a', 'b'], ['b', 'c'], ['c', 'd']], 4);
  assert.deepEqual(multiFailureImpact(path.nodes, path.edges, ['b', 'c']), { removed: 2, components: 2, largest: 1, isolated: 2 }, 'cutting the middle isolates both ends');
  assert.deepEqual(multiFailureImpact(path.nodes, path.edges, ['b']), { removed: 1, components: 2, largest: 2, isolated: 1 });
  const tail = build([['a', 'b'], ['b', 'c'], ['c', 'a'], ['c', 'd'], ['d', 'e']], 5);
  assert.deepEqual(multiFailureImpact(tail.nodes, tail.edges, ['c', 'd']), { removed: 2, components: 2, largest: 2, isolated: 1 }, 'the triangle remnant survives, E falls out isolated');
  assert.equal(multiFailureImpact(path.nodes, path.edges, ['zzz']), null, 'unknown ids are ignored, never silently tested');
  assert.equal(multiFailureImpact(path.nodes, path.edges, []), null);
  assert.equal(multiFailureImpact(path.nodes, path.edges, ['a', 'b', 'c', 'd'], 2)?.removed, 2, 'the selection is bounded');
  const withGroup = [...path.nodes, { ...path.nodes[0], id: 'group:x', group: true }];
  assert.deepEqual(multiFailureImpact(withGroup, path.edges, ['group:x']), null, 'LoD display groups are not entities');
});

test('wave 14 saved views are sanitized, bounded and sorted like a case folder', () => {
  assert.equal(normalizeSavedName('  Fall\u0000 Nordlicht  '), 'Fall Nordlicht', 'control characters never survive');
  assert.equal(normalizeSavedName('x'.repeat(90)).length, 60, 'names are capped');
  assert.equal(normalizeSavedName(42), '');
  const list = readSavedViews(JSON.stringify([
    { name: 'Zeta', query: 'mode=network', saved: '2026-10-20T10:00:00Z' },
    { name: '  alpha  ', query: 'mode=list&q=test', saved: '2026-10-19' },
    { name: 'Alpha', query: 'mode=map', saved: '2026-10-21' },
    { name: '', query: 'mode=list', saved: '2026-10-21' },
    { name: 'Broken', query: '', saved: '2026-10-21' },
    { name: 'Undated', query: 'mode=list', saved: 'irgendwann' },
  ]));
  assert.deepEqual(list.map(entry => entry.name), ['alpha', 'Zeta'], 'sorted by name, blanks and dupes dropped');
  assert.deepEqual(readSavedViews('kein json'), []);
  assert.deepEqual(readSavedViews(null), []);
  const many = Array.from({ length: 80 }, (_, i) => ({ name: `view-${String(i).padStart(2, '0')}`, query: 'mode=list', saved: '2026-10-20' }));
  assert.equal(readSavedViews(JSON.stringify(many)).length, SAVED_VIEW_LIMIT, 'the store stays bounded');

  let views = upsertSavedView([], 'Fall Nordlicht', 'mode=list&q=nordlicht', '2026-10-20T08:00:00Z');
  assert.deepEqual(views.map(entry => [entry.name, entry.query, entry.saved]), [['Fall Nordlicht', 'mode=list&q=nordlicht', '2026-10-20']], 'dates normalize to the day');
  views = upsertSavedView(views, 'fall nordlicht', 'mode=network', '2026-10-21');
  assert.equal(views.length, 1, 'the same name overwrites instead of duplicating');
  assert.equal(views[0].query, 'mode=network');
  views = upsertSavedView(views, 'Alpha', 'mode=map', '2026-10-21');
  assert.deepEqual(views.map(entry => entry.name), ['Alpha', 'fall nordlicht'], 'the list stays sorted');
  assert.equal(upsertSavedView(views, '   ', 'mode=map', '2026-10-21'), views, 'a blank name is a no-op');
  assert.equal(upsertSavedView(views, 'Neu', 'mode=map', 'kein Datum'), views, 'an invalid date is a no-op');
  assert.deepEqual(removeSavedView(views, 'ALPHA').map(entry => entry.name), ['fall nordlicht'], 'deleting is case-insensitive');
  assert.deepEqual(removeSavedView(views, 'fehlt').map(entry => entry.name), ['Alpha', 'fall nordlicht'], 'unknown names change nothing');
});

test('wave 14 evidence export lists documents per entity and relationship', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'n1', name: 'Nordlicht', evidenceIds: ['d1'] }, { key: 'n2', name: 'Hansa' }],
    evidence: [
      { id: 'd2', title: 'Presse' },
      { id: 'd1', title: 'Register, Handelsregister', source: 'HR', date: '2021-01-01', url: 'https://x.test/1' },
    ],
    edges: [{ source: 'n1', target: 'n2', evidenceIds: ['d1', 'd2'] }],
  });
  const csv = evidenceToCSV(data, data.nodes, data.edges);
  const lines = csv.trimEnd().split('\n');
  assert.equal(lines[0], 'id,title,source,date,url,used_by');
  assert.equal(lines[1], 'd1,"Register, Handelsregister",HR,2021-01-01,https://x.test/1,Nordlicht | Nordlicht → Hansa', 'commas are quoted and entity plus relationship uses are listed');
  assert.ok(lines[2].startsWith('d2,Presse,,,,Nordlicht → Hansa'), 'a document only used by a relationship lists just that use, undated stays empty');
  assert.equal(evidenceToCSV(data, data.nodes, data.edges), csv, 'deterministic');
  assert.equal(evidenceToCSV(normalizeDataset({ nodes: [{ key: 'a', name: 'A' }], edges: [], evidence: [] }), [], []), 'id,title,source,date,url,used_by\n', 'an empty slice exports the header only');
  assert.equal(evidenceToCSV(data, data.nodes, data.edges, 1).trimEnd().split('\n').length, 2, 'the export is bounded');
});

test('wave 15 case report notes survive the roundtrip and never overwrite silently', () => {
  const stats = { totalNodes: 2, totalEdges: 1, filteredNodes: 2, filteredEdges: 1, locatedNodes: 0, grouped: false, level: 'Einzelknoten', focused: false, truncated: false };
  const nodes = normalizeDataset({ nodes: [{ key: 'a', name: 'Alpha' }, { key: 'b', name: 'Beta' }], edges: [], evidence: [] }).nodes;
  const notes = { a: 'Zeile eins\nZeile zwei', b: 'nur eine Zeile' };
  for (const lang of ['de', 'en'] as const) {
    const report = caseReport({ stats, nodes, edges: [], notes }, '2026-10-20', lang);
    assert.deepEqual(parseCaseNotes(report), notes, `the ${lang} report round-trips its notes`);
  }
  assert.deepEqual(parseCaseNotes('# irgendwas\n\n## Analytik\n'), {}, 'a report without the notes section yields nothing');
  assert.deepEqual(parseCaseNotes(''), {});
  assert.deepEqual(parseCaseNotes('## Ermittlungsnotizen (2)\n\n_Keine Notizen erfasst._\n'), {}, 'an empty section stays empty');
  const forged = '## Ermittlungsnotizen (1)\n\n- **Nicht im Ausschnitt** (`fremd`)\n  Text von anderswo\n\n## Analytik\n';
  assert.deepEqual(parseCaseNotes(forged), { fremd: 'Text von anderswo' }, 'a foreign id is read honestly, filtered later during use');
  const big = `## Ermittlungsnotizen (1)\n\n- **Big** (\`big\`)\n  ${'x'.repeat(4000)}\n`;
  assert.equal(parseCaseNotes(big).big.length, NOTE_LIMIT, 'imported notes are re-capped');

  const merged = mergeNotes({ a: 'eigener Text' }, { a: 'importierter Text', b: 'neu', c: 'eigener Text' });
  assert.equal(merged.notes.a, 'eigener Text', 'the local note wins over a differing import');
  assert.equal(merged.notes.b, 'neu', 'empty slots are filled');
  assert.equal(merged.notes.c, 'eigener Text', 'untouched ids are filled as well');
  assert.deepEqual([merged.added, merged.unchanged, merged.skipped], [2, 0, 1], 'two new slots, one skipped conflict');
  assert.equal(mergeNotes({ a: 'gleich' }, { a: 'gleich' }).unchanged, 1, 'identical text is not reported as a change');
  assert.deepEqual(mergeNotes({}, {}).notes, {});
  assert.deepEqual(mergeNotes({ x: 'keep' }, { '': 'leer', y: '   ' }), { notes: { x: 'keep' }, added: 0, unchanged: 0, skipped: 0 }, 'junk entries are dropped');
  const deterministic = mergeNotes({ a: 'eins' }, { b: 'zwei', c: 'drei' });
  assert.deepEqual(mergeNotes({ a: 'eins' }, { b: 'zwei', c: 'drei' }), deterministic);
});

test('wave 16 note index searches labels and note text with honest snippets', () => {
  const nodes = normalizeDataset({
    nodes: [{ key: 'a', name: 'Nordlicht GmbH' }, { key: 'b', name: 'Hansa AG' }], edges: [], evidence: [],
  }).nodes;
  const notes = {
    a: 'Geschäftsführerin prüfen\nZweite Quelle fehlt für den Eigentümer',
    b: 'Registerauszug bestellt',
    ghost: 'Knoten nicht mehr im Ausschnitt',
  };
  assert.deepEqual(searchNoteIndex(nodes, notes, '').map(hit => hit.id), ['ghost', 'b', 'a'], 'empty query is a case overview, sorted by label');
  const label = searchNoteIndex(nodes, notes, 'hansa');
  assert.deepEqual(label.map(hit => [hit.id, hit.where]), [['b', 'label']]);
  const both = searchNoteIndex(nodes, notes, 'nordlicht');
  assert.equal(both[0].where, 'label');
  const text = searchNoteIndex(nodes, notes, 'eigentümer');
  assert.equal(text[0].id, 'a', 'note text matches too');
  assert.equal(text[0].snippet, 'Zweite Quelle fehlt für den Eigentümer', 'the matching line becomes the snippet, not the first one');
  assert.equal(text[0].where, 'note');
  assert.deepEqual(searchNoteIndex(nodes, notes, 'zzz'), []);
  assert.equal(searchNoteIndex(nodes, notes, 'knoten')[0].label, 'ghost', 'a note whose entity vanished keeps its raw id as label');
  const ranked = searchNoteIndex(nodes, { a: 'hansa', b: 'hansa' }, 'hansa');
  assert.deepEqual(ranked.map(hit => [hit.id, hit.where]), [['b', 'both'], ['a', 'note']], 'label hits rank above note-only hits, both-match hits first');
  const long = searchNoteIndex(nodes, { a: 'x'.repeat(400) }, 'xxx');
  assert.equal(long[0].snippet.length, 140, 'snippets are bounded');
  assert.ok(long[0].snippet.endsWith('…'));
  assert.equal(searchNoteIndex(nodes, notes, '', 1).length, 1, 'the index is bounded');
  assert.deepEqual(searchNoteIndex(nodes, notes, ''), searchNoteIndex(nodes, notes, ''), 'deterministic');
});

test('wave 16 case bundle is a deterministic, versioned envelope', () => {
  const bundle = caseBundle({
    notes: { b: 'zwei', a: '  eins  ', leer: '   ' },
    views: [
      { name: 'Zeta', query: 'mode=list', saved: '2026-10-20T10:00:00Z' },
      { name: 'alpha', query: 'mode=network', saved: '2026-10-19' },
      { name: '', query: 'mode=map', saved: '2026-10-19' },
    ],
    report: '# Fallbericht\n',
    generated: '2026-10-20T12:00:00.000Z',
  });
  const parsed = JSON.parse(bundle);
  assert.equal(parsed.format, 'puppetnet-case');
  assert.equal(parsed.version, 1);
  assert.equal(parsed.generated, '2026-10-20T12:00:00.000Z');
  assert.deepEqual(Object.keys(parsed.notes), ['a', 'b'], 'notes are sorted and empty ones drop out');
  assert.equal(parsed.notes.a, 'eins', 'notes stay normalized');
  assert.deepEqual(parsed.views.map((view: { name: string }) => view.name), ['alpha', 'Zeta'], 'sessions are sorted and unnamed ones drop out');
  assert.equal(parsed.views[1].saved, '2026-10-20', 'dates normalize');
  assert.equal(parsed.report, '# Fallbericht\n');
  assert.equal(caseBundle({
    notes: { b: 'zwei', a: '  eins  ', leer: '   ' }, report: '# Fallbericht\n', generated: '2026-10-20T12:00:00.000Z',
    views: [{ name: 'alpha', query: 'mode=network', saved: '2026-10-19' }, { name: 'Zeta', query: 'mode=list', saved: '2026-10-20T10:00:00Z' }],
  }), bundle, 'key and session order never change the bytes');
  assert.equal(JSON.parse(caseBundle({ notes: {}, views: [], report: '', generated: '2026-10-20' })).format, 'puppetnet-case', 'an empty case still produces a valid bundle');
});

test('wave 17 provenance gaps count sources and flag thin evidence', () => {
  const data = normalizeDataset({
    nodes: [
      { key: 'a', name: 'Alpha', evidenceIds: ['d1'] },
      { key: 'b', name: 'Beta' },
      { key: 'c', name: 'Gamma', evidenceIds: ['d1'] },
    ],
    evidence: [{ id: 'd1', title: 'Register' }],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', confidence: 0.9, evidenceIds: ['d1'] },
      { source: 'a', target: 'c', type: 'OWNS', confidence: 0.4, evidenceIds: ['d1'] },
      { source: 'b', target: 'c', type: 'OWNS', confidence: 0.95 },
      { source: 'a', target: 'c', type: 'MET', confidence: 0.9, evidenceIds: ['d1', 'd2'] },
    ],
  });
  const gaps = provenanceGaps(data.nodes, data.edges);
  assert.deepEqual([gaps.nodes, gaps.nodesBacked, gaps.edges, gaps.edgesBacked], [3, 2, 4, 3]);
  assert.deepEqual(gaps.singleSource.map(edge => edge.id), ['edge:0'], 'only a high-confidence edge with exactly one source counts');
  assert.equal(gaps.singleSourceCount, 1);
  assert.equal(gaps.unbacked[0].id, 'b', 'an entity without any document is listed');
  assert.equal(gaps.unbackedNodeCount, 1);
  assert.equal(gaps.unbacked[0].type, 'Other');
  assert.deepEqual(provenanceGaps(data.nodes, data.edges), gaps, 'deterministic');
  const clean = normalizeDataset({
    nodes: [{ key: 'a', name: 'A', evidenceIds: ['d1'] }],
    evidence: [{ id: 'd1', title: 'D' }], edges: [],
  });
  const none = provenanceGaps(clean.nodes, clean.edges);
  assert.deepEqual([none.singleSourceCount, none.unbackedNodeCount, none.nodesBacked], [0, 0, 1], 'a clean slice reports honest zeros, not null');
  const low = normalizeDataset({
    nodes: [{ key: 'a', name: 'A' }, { key: 'b', name: 'B' }],
    edges: [{ source: 'a', target: 'b', confidence: 0.2, evidenceIds: ['only'] }], evidence: [],
  });
  assert.equal(provenanceGaps(low.nodes, low.edges).singleSourceCount, 0, 'low-confidence single-source links are not nagged about');
  const grouped = [{ ...data.nodes[0], id: 'group:x', group: true }, ...data.nodes];
  assert.equal(provenanceGaps(grouped, data.edges).nodes, 3, 'LoD display groups are not claims');
  const aggregated = [...data.edges, { ...data.edges[0], id: 'agg', type: 'AGGREGATED' }];
  assert.equal(provenanceGaps(data.nodes, aggregated).edges, 4, 'aggregated LoD edges are excluded');
  assert.equal(provenanceGaps(data.nodes, data.edges, 1).singleSource.length, 1, 'the lists are bounded');
});

test('wave 17 case bundles read back safely or not at all', () => {
  const bundle = caseBundle({
    notes: { a: 'eins', b: '  zwei  ' },
    views: [{ name: 'Fall Nordlicht', query: 'mode=list&q=nordlicht', saved: '2026-10-20T09:00:00Z' }],
    report: '# PuppetNET — Fallbericht\n',
    generated: '2026-10-20T12:00:00.000Z',
  });
  const read = readCaseBundle(bundle);
  assert.deepEqual(read?.notes, { a: 'eins', b: 'zwei' }, 'notes are re-sanitized on read');
  assert.deepEqual(read?.views.map(view => [view.name, view.query, view.saved]), [['Fall Nordlicht', 'mode=list&q=nordlicht', '2026-10-20']]);
  assert.equal(read?.report, '# PuppetNET — Fallbericht\n');
  assert.equal(readCaseBundle('kein json'), null, 'junk is rejected');
  assert.equal(readCaseBundle('{}'), null);
  assert.equal(readCaseBundle(JSON.stringify({ format: 'puppetnet-case' })), null, 'a missing version is rejected');
  assert.equal(readCaseBundle(JSON.stringify({ format: 'puppetnet-case', version: 2, notes: {}, views: [], report: '' })), null, 'a future version is rejected');
  const minimal = readCaseBundle(JSON.stringify({ format: 'puppetnet-case', version: 1 }));
  assert.deepEqual([minimal?.notes, minimal?.views, minimal?.report], [{}, [], ''], 'a sparse bundle reads as empty, not as a crash');
  const hostile = readCaseBundle(JSON.stringify({ format: 'puppetnet-case', version: 1, notes: { a: 'x'.repeat(5000), '': 'leer' }, views: [{ name: '', query: '', saved: '' }], report: 42 }));
  assert.equal(hostile?.notes.a.length, NOTE_LIMIT, 'oversized notes are capped');
  assert.deepEqual(hostile?.views, [], 'invalid sessions drop out');
  assert.equal(hostile?.report, '', 'a non-string report never reaches the UI');
  const roundtrip = readCaseBundle(bundle);
  assert.equal(caseBundle({ notes: roundtrip!.notes, views: roundtrip!.views, report: roundtrip!.report, generated: '2026-10-20T12:00:00.000Z' }), bundle, 'bundle -> read -> bundle is stable');
});

test('wave 18 source verdict tells independence apart from a document count', () => {
  const docs = (list: unknown[]) => normalizeEvidence(list);
  assert.deepEqual(edgeSources([]), { documents: 0, sources: 0, independence: 'none' });
  assert.deepEqual(edgeSources(docs([{ id: 'd1', title: 'A', source: 'HR' }])), { documents: 1, sources: 1, independence: 'single' });
  assert.equal(edgeSources(docs([{ id: 'd1', title: 'A', source: 'Handelsblatt' }, { id: 'd2', title: 'B', source: '  handelsblatt ' }])).independence, 'shared',
    'two documents from the same outlet are not corroboration');
  assert.equal(edgeSources(docs([{ id: 'd1', title: 'A' }, { id: 'd2', title: 'B' }])).independence, 'unknown',
    'documents without a source name can never prove independence');
  const independent = edgeSources(docs([{ id: 'd1', title: 'A', source: 'Handelsregister' }, { id: 'd2', title: 'B', source: 'Presse' }]));
  assert.deepEqual(independent, { documents: 2, sources: 2, independence: 'independent' });
  assert.equal(edgeSources(docs([{ id: 'd1', title: 'A', source: 'HR' }, { id: 'd1', title: 'dup' }])).documents, 1, 'the same document twice is one document');
});

test('wave 18 shared documents group the entities they back', () => {
  const data = normalizeDataset({
    nodes: [
      { key: 'a', name: 'Alpha GmbH', evidenceIds: ['d1'] },
      { key: 'b', name: 'Beta AG', evidenceIds: ['d1', 'd2'] },
      { key: 'c', name: 'Gamma', evidenceIds: ['d2'] },
      { key: 'd', name: 'Delta', evidenceIds: ['d3'] },
    ],
    evidence: [
      { id: 'd1', title: 'Registerauszug', source: 'HR' },
      { id: 'd2', title: 'Zeitungsartikel' },
      { id: 'd3', title: 'Einzelbeleg' },
    ],
    edges: [],
  });
  const groups = sharedSourceGroups(data.nodes, data.evidence);
  assert.deepEqual(groups.map(group => [group.document, group.entities.map(entity => entity.id)]), [['d1', ['a', 'b']], ['d2', ['b', 'c']]],
    'documents backing a single entity are not groups');
  assert.equal(groups[0].title, 'Registerauszug');
  assert.equal(groups[0].source, 'HR');
  assert.equal(groups[1].source, undefined, 'a document without a source name stays honest');
  assert.deepEqual(sharedSourceGroups(data.nodes, data.evidence), groups, 'deterministic');
  const grouped = [{ ...data.nodes[0], id: 'group:x', group: true, evidenceIds: ['d1'] }, ...data.nodes];
  assert.equal(sharedSourceGroups(grouped, data.evidence)[0].entities.length, 2, 'LoD display groups are not entities');
  assert.equal(sharedSourceGroups(data.nodes, data.evidence, 1).length, 1, 'the list is bounded');
  assert.deepEqual(sharedSourceGroups([], []), []);
});

test('wave 19 evidence-quality filter narrows claims, never entities', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'A' }, { key: 'b', name: 'B' }, { key: 'c', name: 'C' }],
    evidence: [
      { id: 'd1', title: 'Register', source: 'HR' },
      { id: 'd2', title: 'Artikel', source: 'Presse' },
      { id: 'd3', title: 'Blog' },
      { id: 'd4', title: 'Blog 2' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['d1', 'd2'] },
      { source: 'b', target: 'c', type: 'OWNS', evidenceIds: ['d1'] },
      { source: 'a', target: 'c', type: 'MET', evidenceIds: ['d3', 'd4'] },
    ],
  });
  const base: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: false, ratio: 1 };
  const ids = (sources?: 'corroborated' | 'thin') => filterDataset(data, { ...base, sources }).edges.map(edge => edge.id);
  assert.deepEqual(ids(), ['edge:0', 'edge:1', 'edge:2'], 'no filter keeps every claim');
  assert.deepEqual(ids('corroborated'), ['edge:0'], 'only distinct named sources count as corroboration');
  assert.deepEqual(ids('thin'), ['edge:1', 'edge:2'], 'single docs and unnamed pairs are thin, not silent');
  assert.equal(filterDataset(data, { ...base, sources: 'corroborated' }).nodes.length, 3, 'entities are never removed by the claim filter');
  assert.equal(edgeSourceStatus(data.edges[0], data.evidence), 'independent');
  assert.equal(edgeSourceStatus(data.edges[1], data.evidence), 'single');
  assert.equal(edgeSourceStatus(data.edges[2], data.evidence), 'unknown', 'two unnamed documents are not independent');
  const nobody = filterDataset(data, base);
  assert.deepEqual(filterDataset(data, { ...base, sources: 'thin' }).evidence, nobody.evidence, 'the evidence table is never reduced');
  assert.equal(encodeViewState({ sources: 'thin' }), 'sources=thin');
  assert.equal(decodeViewState('?sources=corroborated').sources, 'corroborated');
  assert.equal(decodeViewState('?sources=junk').sources, undefined, 'junk in the URL is ignored');
  assert.equal(encodeViewState({}), '');
});

test('wave 20 source registry aggregates outlets deterministically', () => {
  const build = (evidence: unknown[]) => normalizeDataset({
    nodes: [
      { key: 'a', name: 'Alpha', evidenceIds: ['d1'] },
      { key: 'b', name: 'Beta', evidenceIds: ['d1', 'd2'] },
      { key: 'c', name: 'Gamma', evidenceIds: ['d3'] },
      { key: 'd', name: 'Delta' },
    ],
    evidence,
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['d1'] },
      { source: 'b', target: 'c', type: 'MET', evidenceIds: ['d2', 'd3'] },
    ],
  });
  const evidence = [
    { id: 'd1', title: 'R1', source: 'Handelsregister', date: '2020-01-05' },
    { id: 'd2', title: 'A1', source: ' Presseschau ', date: '2021-03-01' },
    { id: 'd3', title: 'A2', source: 'Presseschau', date: '2019-11-20' },
    { id: 'd4', title: 'Blog' },
  ];
  const data = build(evidence);
  const registry = sourceRegistry(data, data.nodes, data.edges);
  assert.deepEqual(registry.map(entry => entry.name), ['Presseschau', 'Handelsregister'], 'documents first, then the stable name order');
  assert.deepEqual([registry[0].documents, registry[0].entities, registry[0].edges], [2, 2, 1]);
  assert.deepEqual([registry[0].first, registry[0].last], ['2019-11-20', '2021-03-01'], 'the outlet window spans its documents');
  assert.equal(registry[1].edges, 1);
  assert.ok(!registry.some(entry => entry.name === ''), 'documents nobody backs are not invented as an outlet');
  const reversed = build([...evidence].reverse());
  assert.deepEqual(sourceRegistry(reversed, reversed.nodes, reversed.edges), registry, 'evidence order never changes the registry');
  const unnamed = build([{ id: 'd1', title: 'Blog' }, { id: 'd2', title: 'Blog 2' }]);
  assert.deepEqual(sourceRegistry(unnamed, unnamed.nodes, unnamed.edges).map(entry => [entry.name, entry.documents]), [['', 3]],
    'unnamed documents are reported, and a referenced-but-unresolved document counts as unnamed too');
  assert.equal(sourceRegistry(data, data.nodes, data.edges, 1).length, 1, 'the registry is bounded');
});

test('wave 20 outlet filter shows one source and nothing else', () => {
  const data = normalizeDataset({
    nodes: [
      { key: 'a', name: 'Alpha', evidenceIds: ['d1'] },
      { key: 'b', name: 'Beta', evidenceIds: ['d1', 'd2'] },
      { key: 'c', name: 'Gamma', evidenceIds: ['d3'] },
      { key: 'd', name: 'Delta' },
    ],
    evidence: [
      { id: 'd1', title: 'R1', source: 'Handelsregister' },
      { id: 'd2', title: 'A1', source: 'Presse' },
      { id: 'd3', title: 'A2', source: 'Presse' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['d1'] },
      { source: 'b', target: 'c', type: 'MET', evidenceIds: ['d2', 'd3'] },
    ],
  });
  const base: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: false, ratio: 1 };
  const presse = filterDataset(data, { ...base, outlet: 'presse' });
  assert.deepEqual(presse.edges.map(edge => edge.id), ['edge:1'], 'only edges this outlet documents');
  assert.deepEqual(presse.nodes.map(node => node.id), ['b', 'c'], 'entities come from its documents and the kept edges');
  const register = filterDataset(data, { ...base, outlet: '  HANDELSREGISTER ' });
  assert.deepEqual(register.edges.map(edge => edge.id), ['edge:0'], 'matching is case- and whitespace-insensitive');
  assert.deepEqual(register.nodes.map(node => node.id), ['a', 'b']);
  const nobody = filterDataset(data, { ...base, outlet: 'gibt es nicht' });
  assert.deepEqual([nobody.nodes.length, nobody.edges.length], [0, 0], 'an unknown outlet is empty, not silently ignored');
  assert.equal(filterDataset(data, base).nodes.length, 4, 'no outlet means no narrowing');
  const combined = filterDataset(data, { ...base, outlet: 'presse', sources: 'corroborated' });
  assert.deepEqual([combined.nodes.length, combined.edges.length], [2, 0],
    'filters compose honestly: the outlet keeps its two entities, corroboration drops the single-outlet edge');
  assert.equal(encodeViewState({ outlet: 'Presse' }), 'outlet=Presse');
  assert.equal(decodeViewState('?outlet=Presse').outlet, 'Presse');
  assert.equal(decodeViewState(`?outlet=${'x'.repeat(200)}`).outlet, undefined, 'oversized outlet names are ignored');
});

test('wave 21 conflicting claims separate contradictions from developments', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'Alpha' }, { key: 'b', name: 'Beta' }, { key: 'c', name: 'Gamma' }, { key: 'd', name: 'Delta' }, { key: 'e', name: 'Epsilon' }],
    evidence: [
      { id: 'old', title: 'R', source: 'HR', date: '2020-01-10' },
      { id: 'new', title: 'A', source: 'Presse', date: '2021-06-01' },
      { id: 'late', title: 'B', source: 'Presse', date: '2023-02-01' },
      { id: 'late2', title: 'B2', source: 'Blog', date: '2023-04-01' },
      { id: 'mid', title: 'C', source: 'Blog', date: '2023-03-01' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['old'] },
      { source: 'b', target: 'a', type: 'SOLD', evidenceIds: ['new'] },
      { source: 'c', target: 'd', type: 'OWNS', evidenceIds: ['late', 'late2'] },
      { source: 'd', target: 'c', type: 'DISPUTED', evidenceIds: ['mid'] },
      { source: 'e', target: 'a', type: 'OWNS', evidenceIds: ['mid'] },
      { source: 'a', target: 'e', type: 'MET', evidenceIds: [] },
    ],
  });
  const conflicts = pairConflicts(data.nodes, data.edges, data.evidence);
  assert.deepEqual(conflicts.map(entry => [entry.pair.join('|'), entry.kind]), [
    ['c|d', 'conflict'],
    ['a|b', 'sequence'],
    ['a|e', 'undated'],
  ], 'contradictions first, then developments, then the undatable');
  assert.deepEqual(conflicts[0].claims.map(claim => [claim.type, claim.documents, claim.sources]), [['DISPUTED', 1, 1], ['OWNS', 2, 2]],
    'the OWNS window 02-01 → 04-01 encloses the DISPUTED day, so both claims cover the same period');
  assert.equal(conflicts[1].claims[0].first, '2020-01-10', 'the window travels with the claim');
  assert.deepEqual(conflicts[1].labels, ['Alpha', 'Beta'], 'labels resolve, direction is ignored');
  const same = normalizeDataset({
    nodes: [{ key: 'x', name: 'X' }, { key: 'y', name: 'Y' }],
    evidence: [{ id: 'd1', title: 'R', source: 'HR', date: '2024-01-01' }, { id: 'd2', title: 'A', source: 'Presse', date: '2024-02-01' }],
    edges: [{ source: 'x', target: 'y', type: 'OWNS', evidenceIds: ['d1'] }, { source: 'y', target: 'x', type: 'OWNS', evidenceIds: ['d2'] }],
  });
  assert.deepEqual(pairConflicts(same.nodes, same.edges, same.evidence), [], 'the same type from two sources is corroboration, not a conflict');
  assert.equal(pairConflicts(data.nodes, data.edges, data.evidence, 1).length, 1, 'the list is bounded');
  assert.deepEqual(pairConflicts(data.nodes, data.edges, data.evidence), conflicts, 'deterministic');
});

test('wave 21 evidence currency separates old from unknown', () => {
  assert.equal(monthsBefore('2026-01-31', 1), '2025-12-31', 'the day of month is clamped, never rolled over');
  assert.equal(monthsBefore('2026-03-31', 1), '2026-02-28');
  assert.equal(monthsBefore('2024-03-31', 1), '2024-02-29', 'leap years are real');
  assert.equal(monthsBefore('2026-10-21', 12), '2025-10-21');
  assert.equal(monthsBefore('kein Datum', 2), null);
  const data = normalizeDataset({
    nodes: [
      { key: 'old', name: 'Old', evidenceIds: ['d1'] },
      { key: 'fresh', name: 'Fresh', evidenceIds: ['d2'] },
      { key: 'undated', name: 'Undated', evidenceIds: ['d3'] },
      { key: 'nothing', name: 'Nothing' },
    ],
    evidence: [
      { id: 'd1', title: 'A', date: '2020-01-10' },
      { id: 'd2', title: 'B', date: '2026-05-01' },
      { id: 'd3', title: 'C' },
    ],
    edges: [],
  });
  const freshness = evidenceFreshness(data.nodes, data.evidence, '2026-10-21');
  assert.deepEqual([freshness?.reference, freshness?.months], ['2026-10-21', 12]);
  assert.deepEqual([freshness?.staleCount, freshness?.undatedCount, freshness?.entities], [1, 2, 4]);
  assert.deepEqual(freshness?.stale.map(entry => [entry.id, entry.last]), [['old', '2020-01-10']]);
  assert.equal(freshness?.stale[0].label, 'Old');
  assert.deepEqual(evidenceFreshness(data.nodes, data.evidence, '2026-10-21'), freshness, 'deterministic');
  assert.equal(evidenceFreshness(data.nodes, data.evidence, 'junk'), null, 'an unusable reference is null, not a silent default');
  const recent = evidenceFreshness(data.nodes, data.evidence, '2026-10-21', 120);
  assert.equal(recent?.staleCount, 0, 'a wider window counts nothing as stale');
  assert.equal(evidenceFreshness([], [], '2026-10-21')?.entities, 0);
  const grouped = [{ ...data.nodes[0], id: 'group:x', group: true, evidenceIds: ['d1'] }, ...data.nodes];
  assert.equal(evidenceFreshness(grouped, data.evidence, '2026-10-21')?.entities, 4, 'LoD display groups are not entities');
});

test('wave 22 dispute view narrows to pairs that genuinely clash', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'Alpha' }, { key: 'b', name: 'Beta' }, { key: 'c', name: 'Gamma' }, { key: 'd', name: 'Delta' }],
    evidence: [
      { id: 'e1', title: 'R', source: 'HR', date: '2024-01-10' },
      { id: 'e2', title: 'A', source: 'Presse', date: '2024-06-01' },
      { id: 'e3', title: 'B', source: 'Blog', date: '2024-04-01' },
      { id: 'e4', title: 'C', source: 'Blog', date: '2024-07-01' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['e1', 'e2'] },
      { source: 'b', target: 'a', type: 'SOLD', evidenceIds: ['e3'] },
      { source: 'c', target: 'd', type: 'OWNS', evidenceIds: ['e1'] },
      { source: 'd', target: 'c', type: 'OWNS', evidenceIds: ['e4'] },
    ],
  });
  const base: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: false, ratio: 1 };
  const full = filterDataset(data, base);
  assert.deepEqual([full.nodes.length, full.edges.length], [4, 4], 'the unfiltered slice keeps everything');
  const disputed = filterDataset(data, { ...base, dispute: true });
  assert.deepEqual(disputed.edges.map(edge => `${edge.source}>${edge.target}:${edge.type}`), ['a>b:OWNS', 'b>a:SOLD'],
    'both claims of the contested pair stay visible, nothing else does');
  assert.deepEqual(disputed.nodes.map(node => node.id).sort(), ['a', 'b'], 'only the entities in the dispute survive');
  assert.deepEqual(filterDataset(data, { ...base, dispute: true }), disputed, 'stable when applied twice');
  const corroboratedPair = normalizeDataset({
    nodes: [{ key: 'x', name: 'X' }, { key: 'y', name: 'Y' }],
    evidence: [{ id: 'd1', title: 'R', date: '2024-01-01' }, { id: 'd2', title: 'A', date: '2024-02-01' }],
    edges: [{ source: 'x', target: 'y', type: 'OWNS', evidenceIds: ['d1'] }, { source: 'y', target: 'x', type: 'OWNS', evidenceIds: ['d2'] }],
  });
  assert.deepEqual([filterDataset(corroboratedPair, { ...base, dispute: true }).nodes.length, filterDataset(corroboratedPair, { ...base, dispute: true }).edges.length], [0, 0],
    'corroboration is not a dispute, so the view is honestly empty');
  const narrowed = filterDataset(data, { ...base, dispute: true, confidence: 1.1 });
  assert.deepEqual([narrowed.nodes.length, narrowed.edges.length], [0, 0], 'composes with the other filters');
});

test('wave 22 the currency window is a closed vocabulary and survives the permalink', () => {
  assert.deepEqual([...FRESH_WINDOWS], [6, 12, 24, 60]);
  assert.equal(DEFAULT_FRESH_MONTHS, 12);
  assert.ok(isFreshWindow(60) && isFreshWindow(12) && !isFreshWindow(13) && !isFreshWindow('24') && !isFreshWindow(undefined));
  assert.equal(encodeViewState({ fresh: 12 }), '', 'the default window is not spelled out');
  assert.equal(encodeViewState({ fresh: 24, dispute: true }), 'dispute=1&fresh=24');
  assert.deepEqual(decodeViewState('?dispute=1&fresh=24'), { dispute: true, fresh: 24 });
  assert.deepEqual(decodeViewState('?fresh=13'), {}, 'an off-vocabulary window is dropped, never coerced');
  assert.deepEqual(decodeViewState('?dispute=0&fresh=abc'), {});
  assert.deepEqual(decodeViewState('?dispute=1'), { dispute: true });
  const data = normalizeDataset({
    nodes: [{ key: 'old', name: 'Old', evidenceIds: ['d1'] }, { key: 'recent', name: 'Recent', evidenceIds: ['d2'] }],
    evidence: [{ id: 'd1', title: 'A', date: '2024-06-01' }, { id: 'd2', title: 'B', date: '2026-01-01' }],
    edges: [],
  });
  assert.equal(evidenceFreshness(data.nodes, data.evidence, '2026-10-21', 6)?.staleCount, 2, 'a six-month window is stricter');
  assert.equal(evidenceFreshness(data.nodes, data.evidence, '2026-10-21', 24)?.staleCount, 1, 'a two-year window forgives the older entity');
  assert.deepEqual(evidenceFreshness(data.nodes, data.evidence, '2026-10-21', 24)?.stale.map(entry => entry.id), ['old']);
  assert.equal(evidenceFreshness(data.nodes, data.evidence, '2026-10-21', 60)?.staleCount, 0);
});

test('wave 23 backing counts documents and independent sources honestly', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'Alpha' }, { key: 'b', name: 'Beta', evidenceIds: ['d3', 'd4'] }, { key: 'c', name: 'Gamma' }],
    evidence: [
      { id: 'd1', title: 'One', source: 'Handelsblatt', date: '2026-01-10' },
      { id: 'd2', title: 'Two', source: '  handelsblatt ', date: '2025-02-01' },
      { id: 'd3', title: 'Three', source: 'Presse', date: '2024-03-01' },
      { id: 'd4', title: 'Four' },
      { id: 'd5', title: 'Five', source: 'Blog', date: '2023-01-01' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['d1', 'd2'] },
      { source: 'b', target: 'c', type: 'OWNS', evidenceIds: ['d4'] },
      { source: 'c', target: 'a', type: 'OWNS', evidenceIds: ['d5'] },
    ],
  });
  const backing = nodeBacking(data.nodes, data.edges, data.evidence);
  assert.deepEqual(backing.get('a'), { documents: 3, sources: 2, newest: '2026-01-10' },
    'documents arrive via relationships too, and one outlet written twice is one source');
  assert.deepEqual(backing.get('b'), { documents: 4, sources: 2, newest: '2026-01-10' },
    'both incident edges add their documents, so the newest one comes from the a-b edge');
  assert.deepEqual(backing.get('c'), { documents: 2, sources: 1, newest: '2023-01-01' },
    'an unnamed document backs the claim but never becomes a second source');
  assert.deepEqual(nodeBacking([{ ...data.nodes[0], evidenceIds: [] }], [], data.evidence).get('a'), { documents: 0, sources: 0 });
  assert.deepEqual(nodeBacking([], [], []).size, 0);
});

test('wave 23 risk ranks thin-and-old above merely thin', () => {
  const data = normalizeDataset({
    nodes: [
      { key: 'weak', name: 'Weak' }, { key: 'thin', name: 'Thin' }, { key: 'stale', name: 'Stale' },
      { key: 'undated', name: 'Undated' }, { key: 'none', name: 'None' }, { key: 'solid', name: 'Solid' },
    ],
    evidence: [
      { id: 'w', title: 'W', source: 'HR', date: '2024-01-01' },
      { id: 't', title: 'T', source: 'HR', date: '2026-09-01' },
      { id: 's1', title: 'S1', source: 'Presse', date: '2023-05-01' },
      { id: 's2', title: 'S2', source: 'Blog', date: '2023-06-01' },
      { id: 'u1', title: 'U1', source: 'Presse' },
      { id: 'u2', title: 'U2', source: 'Blog' },
      { id: 'g1', title: 'G1', source: 'Presse', date: '2026-07-01' },
      { id: 'g2', title: 'G2', source: 'Blog', date: '2026-08-01' },
    ],
    edges: [
      { source: 'weak', target: 'solid', type: 'OWNS', evidenceIds: ['w'] },
      { source: 'thin', target: 'solid', type: 'OWNS', evidenceIds: ['t'] },
      { source: 'stale', target: 'solid', type: 'OWNS', evidenceIds: ['s1', 's2'] },
      { source: 'undated', target: 'solid', type: 'OWNS', evidenceIds: ['u1', 'u2'] },
    ],
  });
  const report = evidenceRisk(data.nodes, data.edges, data.evidence, '2026-10-21');
  assert.deepEqual(report?.states, { unbacked: 1, weak: 1, thin: 1, stale: 1, undated: 1, solid: 1 },
    'one entity of each kind; the unconnected one has no document at all');
  assert.equal(report?.entities, 6);
  assert.deepEqual(report?.entries.map(entry => [entry.id, entry.state]), [
    ['weak', 'weak'],
    ['stale', 'stale'],
    ['thin', 'thin'],
    ['undated', 'undated'],
    ['none', 'unbacked'],
  ], 'worst first: thin and old, then old, then thin, then undatable, then undocumented');
  assert.equal(report?.weakCount, 5);
  assert.deepEqual(evidenceRisk(data.nodes, data.edges, data.evidence, '2026-10-21'), report, 'deterministic');
  assert.equal(evidenceRisk(data.nodes, data.edges, data.evidence, 'junk'), null);
  assert.equal(evidenceRisk(data.nodes, data.edges, data.evidence, '2026-10-21', 60)?.states.weak, 0, 'a wide window forgives age, not a missing second source');
  assert.equal(evidenceRisk(data.nodes, data.edges, data.evidence, '2026-10-21', 12, 2)?.entries.length, 2, 'bounded');
  const grouped = [...data.nodes.map(node => ({ ...node, id: `g:${node.id}`, group: true, label: 'grp' })), ...data.nodes];
  assert.equal(evidenceRisk(grouped, data.edges, data.evidence, '2026-10-21')?.entities, 6, 'display groups are not entities');
  const csv = riskToCSV(report!);
  assert.ok(csv.startsWith('id,label,type,state,documents,sources,relations,last\n'));
  assert.ok(csv.includes('weak,Weak,Other,weak,1,1,1,2024-01-01'), 'the CSV carries the judgment, not just the raw counts');
  assert.equal(csv.trim().split('\n').length, 6, 'header + the non-solid entries only');
  const nasty = normalizeDataset({ nodes: [{ key: 'x', name: 'X, "quoted"' }], evidence: [{ id: 'q', title: 'Q', source: 'S', date: '2026-01-01' }], edges: [{ source: 'x', target: 'x', type: 'SELF', evidenceIds: ['q'] }] });
  const quoted = riskToCSV(evidenceRisk(nasty.nodes, nasty.edges, nasty.evidence, '2026-10-21')!);
  assert.ok(quoted.includes('"X, ""quoted"""'), 'RFC-4180 quoting is reused');
});

test('wave 23 the thin-file view narrows without touching its own judgment', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'Alpha' }, { key: 'b', name: 'Beta', evidenceIds: ['d3', 'd4'] }, { key: 'c', name: 'Gamma' }],
    evidence: [
      { id: 'd1', title: 'One', source: 'Handelsblatt', date: '2026-01-10' },
      { id: 'd2', title: 'Two', source: 'Presse', date: '2025-02-01' },
      { id: 'd3', title: 'Three', source: 'Blog', date: '2026-03-01' },
      { id: 'd4', title: 'Four' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['d1', 'd2'] },
      { source: 'b', target: 'c', type: 'OWNS', evidenceIds: ['d3', 'd4'] },
    ],
  });
  const base: Filters = { confidence: 0, weight: 0, types: [...TYPES], lod: false, ratio: 1 };
  const narrow = filterDataset(data, { ...base, weak: true });
  assert.deepEqual(narrow.nodes.map(node => node.id), ['c'], 'only the entity with a single independent source survives');
  assert.deepEqual(narrow.edges, [], 'its only relationship pointed at a better-backed entity and must not dangle');
  assert.equal(filterDataset(data, base).nodes.length, 3, 'the unfiltered slice is untouched');
  assert.deepEqual(filterDataset(data, { ...base, weak: true }), narrow, 'stable when applied twice');
  const allThin = normalizeDataset({
    nodes: [{ key: 'x', name: 'X' }, { key: 'y', name: 'Y' }],
    evidence: [{ id: 'z1', title: 'Z1', source: 'HR', date: '2026-01-01' }, { id: 'z2', title: 'Z2' }],
    edges: [{ source: 'x', target: 'y', type: 'OWNS', evidenceIds: ['z1', 'z2'] }],
  });
  const kept = filterDataset(allThin, { ...base, weak: true });
  assert.deepEqual([kept.nodes.length, kept.edges.length], [2, 1], 'two unnamed documents never become a second source');
  assert.equal(encodeViewState({ weak: true }), 'weak=1');
  assert.deepEqual(decodeViewState('?weak=1&dispute=1'), { dispute: true, weak: true });
  assert.deepEqual(decodeViewState('?weak=0'), {});
});

test('wave 24 the diff reports what changed, not what was merely retold', () => {
  const before = normalizeDataset({
    nodes: [{ key: 'a', name: 'Alpha' }, { key: 'b', name: 'Beta' }, { key: 'c', name: 'Gamma' }],
    evidence: [{ id: 'd1', title: 'One', source: 'HR', date: '2025-01-01' }],
    edges: [{ source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['d1'] }],
  });
  const after = normalizeDataset({
    nodes: [{ key: 'a', name: 'Alpha Holding' }, { key: 'b', name: 'Beta' }, { key: 'd', name: 'Delta' }],
    evidence: [{ id: 'd1', title: 'One', source: 'HR', date: '2025-01-01' }, { id: 'd2', title: 'Two', source: 'Presse', date: '2026-02-01' }],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['d1', 'd2'] },
      { source: 'b', target: 'a', type: 'SOLD', evidenceIds: ['d2'] },
    ],
  });
  const diff = diffDatasets(before, after);
  assert.deepEqual(diff.entities, { added: 1, removed: 1, unchanged: 2 }, 'a renamed label is not a new entity and not a removed one');
  assert.deepEqual(diff.relations, { added: 1, removed: 0, unchanged: 1 }, 'a second document on a known relationship is not a new relationship');
  assert.deepEqual(diff.documents, { added: 1, removed: 0, unchanged: 1 });
  assert.deepEqual(diff.newEntities.map(entry => entry.id), ['d']);
  assert.deepEqual(diff.goneEntities.map(entry => entry.id), ['c']);
  assert.deepEqual(diff.newRelations.map(entry => entry.key), ['b|SOLD|a'], 'direction matters: b sold to a is not a owns b');
  assert.deepEqual(diff.newDocuments.map(entry => [entry.id, entry.source]), [['d2', 'Presse']]);
  assert.deepEqual(diff.newConflicts.map(entry => entry.pair.join('|')), ['a|b'], 'the pair only became contradictory in the new state');
  assert.equal(diff.resolvedConflicts, 0);
  assert.deepEqual(diff.corroborated.map(entry => [entry.id, entry.sources]), [['a', 2], ['b', 2]], 'gaining a second independent source is news');
  assert.deepEqual(diffDatasets(after, after).entities, { added: 0, removed: 0, unchanged: 3 }, 'a state is not different from itself');
  assert.deepEqual(diffDatasets(after, after).newConflicts, []);
  assert.deepEqual(diffDatasets(before, after), diff, 'deterministic');
  const reversed = diffDatasets(after, before);
  assert.deepEqual([reversed.entities.added, reversed.entities.removed], [1, 1], 'the diff is symmetric in its counts');
  assert.equal(reversed.resolvedConflicts, 1, 'going back makes the contradiction disappear');
  assert.deepEqual(diffToCSV(diff).split('\n')[0], 'kind,change,ref,label,detail');
  const csv = diffToCSV(diff);
  for (const line of ['entity,added,d,Delta,Other', 'entity,removed,c,Gamma,Other', 'relation,added,b|SOLD|a,,SOLD', 'document,added,d2,Two,Presse', 'conflict,added,a|b,Alpha Holding ↔ Beta,conflict']) {
    assert.ok(csv.includes(line), `CSV carries ${line}`);
  }
  assert.equal(diffDatasets(before, after, 1).newEntities.length, 1, 'lists are bounded while the counts stay complete');
  assert.equal(diffDatasets(before, after, 1).entities.added, 1, 'the count is not truncated');
  const quoted = diffDatasets(
    normalizeDataset({ nodes: [], evidence: [], edges: [] }),
    normalizeDataset({ nodes: [{ key: 'x', name: 'X, "quoted"' }], evidence: [], edges: [] }),
  );
  assert.ok(diffToCSV(quoted).includes('entity,added,x,"X, ""quoted""",Other'), 'RFC-4180 quoting is reused for change rows');
});

test('wave 25 the case search sees everything the file knows, once', () => {
  const data = normalizeDataset({
    nodes: [{ key: 'a', name: 'Alpha Holding' }, { key: 'b', name: 'Beta GmbH' }, { key: 'c', name: 'Gamma AG' }],
    evidence: [
      { id: 'd1', title: 'One', source: 'Handelsblatt', date: '2024-01-01' },
      { id: 'd2', title: 'Two', source: 'Presse', date: '2024-06-01' },
    ],
    edges: [
      { source: 'a', target: 'b', type: 'OWNS', evidenceIds: ['d1'] },
      { source: 'b', target: 'a', type: 'SOLD', evidenceIds: ['d2'] },
    ],
  });
  const input = {
    nodes: data.nodes,
    conflicts: pairConflicts(data.nodes, data.edges, data.evidence),
    risk: evidenceRisk(data.nodes, data.edges, data.evidence, '2026-10-21'),
    sourceRegistry: sourceRegistry(data, data.nodes, data.edges),
    notes: { c: 'Gamma AG taucht nur in einer Quelle auf, Nachrecherche im Handelsregister nötig.' },
  };
  const alpha = searchCase('alpha', input);
  assert.deepEqual(alpha.map(hit => hit.kind), ['entity', 'conflict'], 'an entity hit outranks the contradiction it is part of');
  assert.equal(alpha[0].label, 'Alpha Holding');
  assert.equal(alpha[0].detail, 'stale', 'the sturdiness finding travels as the detail, not as a second row');
  assert.equal(searchCase('sequence', input).length, 1, 'the kind of a contradiction is searchable too');
  assert.equal(searchCase('sequence', input)[0].kind, 'conflict');
  assert.deepEqual(searchCase('handels', input).map(hit => hit.kind), ['outlet', 'note'], 'a source and the note that mentions it');
  assert.deepEqual(searchCase('gamma', input).map(hit => hit.kind), ['entity', 'note'], 'the entity is reported once, the note separately');
  assert.deepEqual(searchCase('zzz', input), []);
  assert.deepEqual(searchCase('   ', input), [], 'an empty query is not a dump of everything');
  assert.equal(searchCase('a', input, 2).length, 2, 'bounded');
  assert.deepEqual(searchCase('beta', input), searchCase('beta', input), 'deterministic');
  assert.deepEqual(searchCase('BETA', input).map(hit => hit.label)[0], 'Beta GmbH', 'case-insensitive');
  const grouped = { ...input, nodes: [{ ...data.nodes[0], id: 'g:a', group: true as const }, ...data.nodes] };
  assert.deepEqual(searchCase('alpha', grouped).map(hit => hit.id), ['a', 'a|b'], 'display groups are not searched');
  assert.deepEqual(searchCase('alpha', { nodes: data.nodes }).map(hit => hit.kind), ['entity'], 'every part of the index is optional');
  const outletHit = searchCase('presse', input).find(hit => hit.kind === 'outlet');
  assert.ok(outletHit && outletHit.detail === '1', 'the outlet hit carries its document count');
  assert.deepEqual(searchCase('alpha', { nodes: data.nodes, notes: { a: 'x'.repeat(500) } }).map(hit => hit.kind), ['entity', 'note'],
    'a note on a matching entity is found through that entity, without an extra index');
  assert.deepEqual(searchCase('zzz', { nodes: data.nodes, notes: { a: 'x'.repeat(500) } }), [],
    'a long note is not a hit unless its text or its entity matches');
});

test('wave 25 note snippets show the match instead of the beginning', () => {
  const long = `${'Vorspann '.repeat(20)}Handelsregister-Eintrag bestätigt die Verbindung${' Anhang'.repeat(20)}`;
  const snippet = noteSnippet(long, 'handelsregister', 40);
  assert.ok(snippet.includes('Handelsregister-Eintrag'), 'the match is inside the excerpt');
  assert.ok(snippet.startsWith('…') && snippet.endsWith('…'), 'both cut sides are marked');
  assert.ok(snippet.length <= 42, 'the excerpt stays short');
  assert.equal(noteSnippet('kurz und knapp', 'kurz', 40), 'kurz und knapp', 'short notes are returned whole');
  assert.equal(noteSnippet(`${'x '.repeat(60)}`, 'fehlt', 30).endsWith('…'), true, 'a miss truncates at the end, never invents context');
  assert.equal(noteSnippet('  viel   Leerraum  ', 'leerraum', 40), 'viel Leerraum', 'whitespace is normalised');
});
