/** KV-free dispatcher and optional public JSON CDN cache. No graph/database API.
 * The isolate counter is advisory only. The Git-persisted workflow budget is
 * authoritative; caches.default is NOT used as a lock or quota database.
 */
export interface Env {
  PAGES_ORIGIN: string;
  ALLOWED_ORIGINS: string;
  GITHUB_OWNER: string;
  GITHUB_REPO: string;
  GITHUB_DISPATCH_TOKEN?: string;
  SEARCH_AUTH_TOKEN?: string;
}
export interface Context { waitUntil(promise: Promise<unknown>): void }
export interface EdgeCache {
  match(key: Request): Promise<Response | undefined>;
  put(key: Request, value: Response): Promise<void>;
}
interface Dependencies { fetch?: typeof fetch; cache?: EdgeCache; now?: () => number }
const JSON_PATH = /^\/data\/(?:graph_latest\.json|static-manifest\.json|entities\/[a-f0-9]{64}\.json)$/;
const STATIC_POLICY = 'public, max-age=60, s-maxage=60, must-revalidate';

async function readBounded(request: Request | Response, limit: number): Promise<Uint8Array> {
  const length = Number(request.headers.get('Content-Length'));
  if (Number.isFinite(length) && length > limit) throw new Error('size');
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > limit) throw new Error('size');
      chunks.push(value);
    }
  } catch (error) { await reader.cancel(); throw error; }
  finally { reader.releaseLock(); }
  const result = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  return result;
}

async function equalToken(a: string, b: string): Promise<boolean> {
  if (a.length > 512 || !a || !b) return false;
  const encode = new TextEncoder();
  const [left, right] = await Promise.all([a, b].map(value => crypto.subtle.digest('SHA-256', encode.encode(value))));
  const x = new Uint8Array(left), y = new Uint8Array(right);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

export function createWorker(deps: Dependencies = {}) {
  const network = deps.fetch ?? globalThis.fetch.bind(globalThis);
  const now = deps.now ?? Date.now;
  let day = '', reservations = 0;
  return {
    async fetch(request: Request, env: Env, ctx: Context): Promise<Response> {
      const url = new URL(request.url);
      const origin = request.headers.get('Origin');
      // No wildcard origin or suffix matching. Preview origins must be explicitly configured.
      const allowed = String(env.ALLOWED_ORIGINS || '').split(',').map(value => value.trim()).filter(Boolean);
      const cors = new Headers({ 'Vary': 'Origin', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer' });
      if (origin && allowed.includes(origin)) cors.set('Access-Control-Allow-Origin', origin);
      function json(data: unknown, status = 200, retry?: string) {
        const headers = new Headers(cors);
        headers.set('Content-Type', 'application/json; charset=utf-8');
        headers.set('Cache-Control', 'no-store');
        if (retry) headers.set('Retry-After', retry);
        return new Response(JSON.stringify(data), { status, headers });
      }
      if (origin && !allowed.includes(origin)) return json({ error: 'origin_not_allowed' }, 403);
      if (url.search) return json({ error: 'query_parameters_not_supported' }, 400);
      if (request.method === 'OPTIONS') {
        if (!origin || !allowed.includes(origin)) return json({ error: 'origin_required' }, 403);
        const method = request.headers.get('Access-Control-Request-Method');
        const methods = url.pathname === '/api/search' ? ['POST'] : JSON_PATH.test(url.pathname) ? ['GET', 'HEAD'] : [];
        if (!method || !methods.includes(method)) return json({ error: 'method_not_allowed' }, 405);
        const requested = (request.headers.get('Access-Control-Request-Headers') || '').toLowerCase().split(',').map(v => v.trim()).filter(Boolean);
        if (requested.some(value => !['authorization', 'content-type'].includes(value))) return json({ error: 'header_not_allowed' }, 403);
        const headers = new Headers(cors);
        headers.set('Access-Control-Allow-Methods', methods.join(', '));
        headers.set('Access-Control-Allow-Headers', 'Authorization, Content-Type');
        headers.set('Access-Control-Max-Age', '600');
        headers.set('Cache-Control', 'no-store');
        return new Response(null, { status: 204, headers });
      }
      if (url.pathname === '/health' && request.method === 'GET') {
        return json({ ok: true, mode: 'static-cdn-and-dispatch', kv: false });
      }
      if (url.pathname === '/api/search') {
        if (request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);
        if (!env.SEARCH_AUTH_TOKEN || env.SEARCH_AUTH_TOKEN.length < 32) return json({ error: 'search_not_configured' }, 503);
        const auth = request.headers.get('Authorization') || '';
        if (!auth.startsWith('Bearer ') || !await equalToken(auth.slice(7), env.SEARCH_AUTH_TOKEN)) return json({ error: 'unauthorized' }, 401);
        if (!env.GITHUB_DISPATCH_TOKEN || !/^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/.test(env.GITHUB_OWNER)
            || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(env.GITHUB_REPO)) return json({ error: 'dispatch_not_configured' }, 503);
        if (!/^application\/json(?:;|$)/i.test(request.headers.get('Content-Type') || '')) return json({ error: 'json_required' }, 415);
        let body: unknown;
        try { body = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(await readBounded(request, 2048))); }
        catch { return json({ error: 'invalid_or_oversized_json' }, 400); }
        if (!body || typeof body !== 'object' || Array.isArray(body)) return json({ error: 'invalid_query' }, 400);
        const raw = (body as Record<string, unknown>).query;
        if (typeof raw !== 'string' || /[\u0000-\u001f\u007f]/.test(raw) || [...raw].length > 120 || !raw.trim()) return json({ error: 'invalid_query' }, 400);
        const today = new Date(now()).toISOString().slice(0, 10);
        if (day !== today) { day = today; reservations = 0; }
        if (reservations >= 2) return json({ error: 'local_dispatch_limit', note: 'Workflow enforces the global daily run budget.' }, 429, '3600');
        reservations++; // reserve synchronously, before awaiting GitHub; failures consume it too
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10_000);
        try {
          const result = await network(`https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/dispatches`, {
            method: 'POST', redirect: 'error', signal: controller.signal,
            headers: { Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`, Accept: 'application/vnd.github+json',
              'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'PuppetNET-Search-Relay' },
            body: JSON.stringify({ event_type: 'puppetnet-refresh', client_payload: { query: raw.trim() } }),
          });
          if (result.status !== 204) {
            await result.body?.cancel();
            return json({ error: 'dispatch_rejected' }, result.status === 429 ? 503 : 502, '60');
          }
          return json({ status: 'accepted', message: 'GitHub accepted the event. Workflow quota, review and deployment still apply.',
            dataPath: '/data/graph_latest.json' }, 202);
        } catch { return json({ error: 'dispatch_unavailable' }, 502); }
        finally { clearTimeout(timer); }
      }
      if (!JSON_PATH.test(url.pathname)) return json({ error: 'not_found' }, 404);
      if (!['GET', 'HEAD'].includes(request.method)) return json({ error: 'method_not_allowed' }, 405);
      let pages: URL;
      try {
        pages = new URL(env.PAGES_ORIGIN);
        if (pages.origin === url.origin || pages.protocol !== 'https:' || pages.username || pages.password || pages.pathname !== '/' || pages.search || pages.hash) throw new Error('origin');
      } catch { return json({ error: 'static_origin_not_configured' }, 503); }
      const target = new URL(url.pathname, pages);
      const key = new Request(target.href, { method: 'GET' });
      // caches.default is optional in local tests; no KV fallback or counter writes.
      const cache = deps.cache ?? (globalThis as unknown as { caches?: { default?: EdgeCache } }).caches?.default;
      let response: Response | undefined;
      try { response = await cache?.match(key); } catch { /* a cache outage is a miss */ }
      if (!response) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10_000);
        try {
          const upstream = await network(target.href, { method: 'GET', credentials: 'omit', redirect: 'error',
            headers: { Accept: 'application/json' }, signal: controller.signal });
          if (upstream.status !== 200 || !/^application\/json(?:;|$)/i.test(upstream.headers.get('Content-Type') || '')) {
            await upstream.body?.cancel();
            return json({ error: 'snapshot_unavailable' }, upstream.status === 404 ? 404 : 502);
          }
          const bytes = await readBounded(upstream, 16 * 1024 * 1024);
          const headers = new Headers({ 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': STATIC_POLICY });
          for (const name of ['ETag', 'Last-Modified']) {
            const value = upstream.headers.get(name); if (value) headers.set(name, value);
          }
          response = new Response(bytes as BodyInit, { headers });
          if (cache) ctx.waitUntil(cache.put(key, response.clone()).catch(() => undefined));
        } catch { return json({ error: 'snapshot_unavailable' }, 502); }
        finally { clearTimeout(timer); }
      }
      const headers = new Headers(response.headers);
      cors.forEach((value, name) => headers.set(name, value));
      return new Response(request.method === 'HEAD' ? null : response.body, { status: 200, headers });
    },
  };
}
export default createWorker();
