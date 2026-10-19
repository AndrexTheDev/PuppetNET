import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createWorker, type Env, type EdgeCache } from '../apps/worker/src/index';

const env: Env = { PAGES_ORIGIN: 'https://puppetnet-web.pages.dev', ALLOWED_ORIGINS: 'https://puppetnet-web.pages.dev',
  GITHUB_OWNER: 'AndrexTheDev', GITHUB_REPO: 'PuppetNET',
  GITHUB_DISPATCH_TOKEN: 'server-only-not-a-real-github-token', SEARCH_AUTH_TOKEN: 'operator-only-test-secret-32-characters' };
const ctx = () => { const tasks: Promise<unknown>[] = []; return { tasks, waitUntil: (task: Promise<unknown>) => { tasks.push(task); } }; };
function search(query: unknown = 'Example Holdings', extra: RequestInit = {}) {
  return new Request('https://relay.test/api/search', { method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${env.SEARCH_AUTH_TOKEN}`, Origin: env.PAGES_ORIGIN },
    body: JSON.stringify({ query }), ...extra });
}

test('authorized dispatch: fixed repo/event, no GitHub secret in response, never cached', async () => {
  let calls = 0;
  const worker = createWorker({ fetch: async (url, init) => {
    calls++; assert.equal(url, 'https://api.github.com/repos/AndrexTheDev/PuppetNET/dispatches');
    assert.equal(new Headers(init?.headers).get('Authorization'), `Bearer ${env.GITHUB_DISPATCH_TOKEN}`);
    assert.equal(init?.redirect, 'error');
    assert.deepEqual(JSON.parse(String(init?.body)), { event_type: 'puppetnet-refresh', client_payload: { query: 'Example Holdings' } });
    return new Response(null, { status: 204 });
  }});
  const response = await worker.fetch(search(), env, ctx());
  assert.equal(response.status, 202); assert.equal(calls, 1);
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  const text = await response.text(); assert.ok(!text.includes(env.GITHUB_DISPATCH_TOKEN!));
  assert.equal(JSON.parse(text).status, 'accepted');
});

test('auth, origin, schema and body limits fail before any GitHub call', async () => {
  const worker = createWorker({ fetch: async () => { throw new Error('must not call'); } });
  assert.equal((await worker.fetch(search('x', { headers: { 'Content-Type': 'application/json' } }), env, ctx())).status, 401);
  const denied = search(); denied.headers.set('Origin', 'https://evil.test');
  assert.equal((await worker.fetch(denied, env, ctx())).status, 403);
  for (const query of ['', 'x'.repeat(121), 'a\nb', null, { query: 'x' }]) {
    assert.equal((await worker.fetch(search(query), env, ctx())).status, 400);
  }
  const oversized = search('x'); oversized.headers.set('Content-Length', '5000');
  assert.equal((await worker.fetch(oversized, env, ctx())).status, 400);
  assert.equal((await worker.fetch(search('x'), { ...env, SEARCH_AUTH_TOKEN: '' }, ctx())).status, 503);
});

test('failed dispatch is not success and is never automatically retried', async () => {
  let calls = 0;
  const worker = createWorker({ fetch: async () => { calls++; return new Response('upstream SECRET', { status: 403 }); } });
  const response = await worker.fetch(search(), env, ctx());
  assert.equal(response.status, 502); assert.equal(calls, 1);
  assert.ok(!(await response.text()).includes('SECRET'));
});

test('advisory local cap reserves before concurrent network calls; resets at UTC day', async () => {
  let now = Date.parse('2026-10-19T03:00:00Z'), calls = 0;
  const worker = createWorker({ now: () => now, fetch: async () => { calls++; return new Response(null, { status: 204 }); } });
  const results = await Promise.all([1, 2, 3].map(() => worker.fetch(search(), env, ctx())));
  assert.deepEqual(results.map(r => r.status), [202, 202, 429]); assert.equal(calls, 2);
  now += 86400000;
  assert.equal((await worker.fetch(search(), env, ctx())).status, 202);
});

test('static forwarding uses caches.default contract and strips all credentials/cookies', async () => {
  const memory = new Map<string, Response>(); let calls = 0;
  const cache: EdgeCache = { match: async key => memory.get(key.url)?.clone(), put: async (key, value) => { memory.set(key.url, value.clone()); } };
  const worker = createWorker({ cache, fetch: async (url, init) => {
    calls++; assert.equal(url, 'https://puppetnet-web.pages.dev/data/graph_latest.json');
    assert.equal(new Headers(init?.headers).get('Authorization'), null);
    assert.equal(new Headers(init?.headers).get('Cookie'), null);
    return new Response('{"nodes":[],"edges":[]}', { headers: { 'Content-Type': 'application/json', 'Set-Cookie': 'secret=yes', ETag: 'revision' } });
  }});
  const request = () => new Request('https://relay.test/data/graph_latest.json', { headers: { Authorization: 'Bearer discarded', Cookie: 'discarded=yes', Origin: env.PAGES_ORIGIN } });
  const context = ctx();
  const first = await worker.fetch(request(), env, context); await Promise.all(context.tasks);
  assert.equal(first.status, 200); assert.match(first.headers.get('Cache-Control')!, /max-age=60/);
  assert.equal(first.headers.get('Set-Cookie'), null);
  const second = await worker.fetch(request(), env, ctx());
  assert.equal(await second.text(), '{"nodes":[],"edges":[]}'); assert.equal(calls, 1);
  assert.equal(second.headers.get('Access-Control-Allow-Origin'), env.PAGES_ORIGIN);
});

test('static allowlist blocks proxy/SSRF paths, errors are not cacheable', async () => {
  let calls = 0;
  const worker = createWorker({ fetch: async () => { calls++; return new Response('missing', { status: 404 }); } });
  for (const path of ['/graph/overview', '/status', '/proxy', '/data/entities/not-a-hash.json', '/data/secrets.json']) {
    assert.equal((await worker.fetch(new Request(`https://relay.test${path}`), env, ctx())).status, 404);
  }
  assert.equal(calls, 0);
  const response = await worker.fetch(new Request('https://relay.test/data/graph_latest.json'), env, ctx());
  assert.equal(response.status, 404); assert.equal(response.headers.get('Cache-Control'), 'no-store');
  assert.equal((await worker.fetch(new Request('https://relay.test/data/graph_latest.json?url=http://127.0.0.1'), env, ctx())).status, 400);
});

test('preflight permits only explicitly configured origins, methods and headers', async () => {
  const worker = createWorker();
  const request = new Request('https://relay.test/api/search', { method: 'OPTIONS', headers: { Origin: env.PAGES_ORIGIN,
    'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type' } });
  const response = await worker.fetch(request, env, ctx());
  assert.equal(response.status, 204); assert.equal(response.headers.get('Access-Control-Allow-Credentials'), null);
  request.headers.set('Access-Control-Request-Headers', 'x-unsafe-header');
  assert.equal((await worker.fetch(request, env, ctx())).status, 403);
});

test('search relay has no bindings, scheduled work or imported historical graph engine', async () => {
  const { readFileSync } = await import('node:fs');
  const config = readFileSync('apps/worker/wrangler.toml', 'utf8');
  const source = readFileSync('apps/worker/src/index.ts', 'utf8');
  assert.match(config, /main = "src\/index\.ts"/);
  assert.doesNotMatch(config, /\[\[kv_namespaces\]\]|"0{32}"|binding\s*=/);
  assert.doesNotMatch(source, /from ['"].*worker\.js|\.scheduled\(|\.KV\.put\(/);
});

test('active provisioning dry run is secret-free; real run with missing secrets fails before account calls', async () => {
  const { spawnSync } = await import('node:child_process');
  const cleanEnv = { ...process.env, GITHUB_DISPATCH_TOKEN: '', SEARCH_AUTH_TOKEN: '' };
  const plan = spawnSync(process.execPath, ['scripts/provision-search-worker.mjs', '--dry-run'], { encoding: 'utf8', env: cleanEnv });
  assert.equal(plan.status, 0); assert.match(plan.stdout, /KV-free/); assert.match(plan.stdout, /no account calls/i);
  const failed = spawnSync(process.execPath, ['scripts/provision-search-worker.mjs'], { encoding: 'utf8', env: cleanEnv });
  assert.equal(failed.status, 1); assert.match(failed.stderr, /No account call/);
});
