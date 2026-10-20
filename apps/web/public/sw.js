/* PuppetNET offline service worker (Wave 6) — analysis without network.
 *
 * Strategies (same-origin GET only, everything else is ignored):
 * - /data/*          stale-while-revalidate: the workspace opens instantly with
 *                    the last published graph and refreshes in the background.
 * - /_next/static/*  cache-first: content-hashed, immutable assets.
 * - navigations      network-first, falling back to the cached shell so an
 *                    offline revisit opens the workspace instead of a browser
 *                    error page.
 * - everything else  network-first with cache fallback.
 *
 * The worker never fabricates data: an offline cache miss behaves exactly like
 * the standby state the dashboard already explains. /sw.js itself is served
 * with Cache-Control: no-store (public/_headers) so a new publication is never
 * pinned by an outdated worker.
 */
const CACHE = 'puppetnet-v1';
const SHELL = ['/dashboard/', '/manifest.webmanifest'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/data/')) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }
  if (url.pathname.startsWith('/_next/static/')) {
    event.respondWith(cacheFirst(request));
    return;
  }
  event.respondWith(networkFirst(request));
});

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((response) => {
      if (response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => cached);
  return cached || network;
}

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(CACHE);
    cache.put(request, response.clone());
  }
  return response;
}

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok && request.mode === 'navigate') {
      const cache = await caches.open(CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await caches.match(request);
    if (cached) return cached;
    if (request.mode === 'navigate') {
      const shell = await caches.match('/dashboard/');
      if (shell) return shell;
    }
    return new Response('', { status: 503, statusText: 'Offline' });
  }
}
