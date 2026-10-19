import { test } from 'node:test';
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { normalizeDataset, demoDataset, filterDataset, groupDataset, geographicPoints, evidenceFor, safeUrl, cypherExport, levelFor, TYPES, type Filters } from '../apps/web/app/dashboard/graph-data';
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
