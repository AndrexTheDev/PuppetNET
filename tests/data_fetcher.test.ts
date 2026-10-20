import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadStaticGraph, searchWorkerOrigin, requestResearch } from '../apps/web/src/lib/dataFetcher';
const graph = { revision: 'test', demo: false, nodes: [{ id: 'org:a', name: 'A', type: 'Organization' }], edges: [] };

test('CDN loader uses same-origin static URL without token or Worker', async () => {
  const result = await loadStaticGraph({ fetcher: async (url, init) => {
    assert.equal(url, '/data/graph_latest.json'); assert.equal(init?.credentials, 'omit');
    assert.equal(new Headers(init?.headers).get('Authorization'), null);
    return new Response(JSON.stringify(graph));
  }});
  assert.equal(result.source, 'static');
  if (result.source === 'static') assert.equal(result.dataset.nodes[0].id, 'org:a');
});

test('offline, missing and malformed JSON give explicitly labelled example fallback', async () => {
  for (const fetcher of [async () => { throw new Error('offline'); }, async () => new Response('not found', { status: 404 }),
    async () => new Response('bad JSON'), async () => new Response('{"nodes":[{}],"edges":[]}')]) {
    const result = await loadStaticGraph({ fetcher });
    assert.equal(result.source, 'demo'); assert.match(result.message, /fiktive/);
  }
});

test('caller cancellation does not silently replace newer data with demo', async () => {
  const controller = new AbortController(); controller.abort();
  await assert.rejects(loadStaticGraph({ signal: controller.signal, fetcher: async () => new Response(JSON.stringify(graph)) }));
});

test('research worker URL must be explicit; no same-origin graph API assumption', () => {
  assert.equal(searchWorkerOrigin('https://relay.example/'), 'https://relay.example');
  for (const url of ['', 'http://relay.example', 'https://user:pass@relay.example', 'https://relay.example/api/search', 'https://relay.example/?token=secret']) assert.throws(() => searchWorkerOrigin(url));
});

test('research sends auth only to explicit search endpoint and expects asynchronous 202', async () => {
  await requestResearch({ workerUrl: 'https://relay.example', token: 'x'.repeat(32), query: 'Example' }, async (url, init) => {
    assert.equal(url, 'https://relay.example/api/search'); assert.equal(init?.method, 'POST');
    assert.equal(init?.credentials, 'omit'); assert.equal(new Headers(init?.headers).get('Authorization'), `Bearer ${'x'.repeat(32)}`);
    assert.deepEqual(JSON.parse(String(init?.body)), { query: 'Example' });
    return new Response('{"status":"accepted"}', { status: 202 });
  });
  await assert.rejects(requestResearch({ workerUrl: 'https://relay.example', token: 'x'.repeat(32), query: 'Example' }, async () => new Response('{}', { status: 200 })));
});

test('build-time profiles prefer the exact same static aggregate over a stale entities.json', async () => {
  const { mkdtempSync, writeFileSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const path = await import('node:path');
  const { loadEntityDataset, resetEntityCache } = await import('../apps/web/lib/entity-store');
  const directory = mkdtempSync(path.join(tmpdir(), 'puppetnet-unified-'));
  try {
    writeFileSync(path.join(directory, 'graph_latest.json'), JSON.stringify(graph));
    writeFileSync(path.join(directory, 'entities.json'), JSON.stringify({ entities: [{ id: 'stale', name: 'Old', type: 'Person' }] }));
    const runtime = await loadStaticGraph({ fetcher: async () => new Response(JSON.stringify(graph)) });
    const profiles = loadEntityDataset(directory);
    assert.equal(runtime.source, 'static');
    if (runtime.source === 'static') assert.deepEqual(profiles.dataset.entities.map(n => n.id), runtime.dataset.nodes.map(n => n.id));
    assert.ok(profiles.file.endsWith('graph_latest.json'));
  } finally { rmSync(directory, { recursive: true, force: true }); resetEntityCache(); }
});
