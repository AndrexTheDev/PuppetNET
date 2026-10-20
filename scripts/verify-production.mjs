#!/usr/bin/env node
/**
 * Read-only production smoke checks for PuppetNET on Cloudflare Pages.
 *
 * Usage:
 *   node scripts/verify-production.mjs
 *   PROD_BASE_URL=https://example.pages.dev node scripts/verify-production.mjs
 *
 * No files are created. Exits non-zero if any production check fails.
 */

const DEFAULT_BASE_URL = 'https://puppetnet-web.pages.dev';
const REQUEST_TIMEOUT_MS = 15_000;
const baseUrl = new URL(process.env.PROD_BASE_URL || DEFAULT_BASE_URL);

if (!['http:', 'https:'].includes(baseUrl.protocol)) {
  console.error(`FAIL Invalid PROD_BASE_URL protocol: ${baseUrl.protocol}`);
  process.exit(2);
}
baseUrl.pathname = baseUrl.pathname.replace(/\/+$/, '');
baseUrl.search = '';
baseUrl.hash = '';

const checks = [];
function record(label, passed, detail = '') {
  checks.push({ label, passed: Boolean(passed), detail });
}

function describeError(error) {
  const cause = error?.cause;
  const reason = cause?.code ? `${cause.code}${cause.host ? ` (${cause.host})` : ''}` : cause?.message;
  return reason ? `${error.message}: ${reason}` : error.message;
}

async function request(path, options = {}) {
  const url = new URL(path, `${baseUrl.origin}${baseUrl.pathname}/`);
  return fetch(url, {
    method: 'GET',
    redirect: 'manual',
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    headers: { 'user-agent': 'PuppetNET-production-verifier/1.0', ...options.headers },
    ...options,
  });
}

function isSchemaOrgContext(context) {
  const contexts = Array.isArray(context) ? context : [context];
  return contexts.some((value) => value === 'https://schema.org' || value === 'https://schema.org/');
}

function hasTypedSchemaNode(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  if (typeof value['@type'] === 'string' || (Array.isArray(value['@type']) && value['@type'].length > 0)) return true;
  return Array.isArray(value['@graph']) && value['@graph'].some((node) =>
    node && typeof node === 'object'
      && (typeof node['@type'] === 'string' || (Array.isArray(node['@type']) && node['@type'].length > 0)));
}

function inspectJsonLd(html) {
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)];
  const jsonLdBlocks = scripts
    .filter(([, attributes]) => /\btype\s*=\s*(?:"application\/ld\+json"|'application\/ld\+json'|application\/ld\+json(?=\s|$))/i.test(attributes))
    .map(([, , body]) => body.trim());

  if (jsonLdBlocks.length === 0) {
    return { valid: false, detail: 'Kein script[type="application/ld+json"] auf der Hauptseite gefunden.' };
  }

  const issues = [];
  let schemaOrgBlockCount = 0;
  for (let index = 0; index < jsonLdBlocks.length; index += 1) {
    let data;
    try {
      data = JSON.parse(jsonLdBlocks[index]);
    } catch (error) {
      issues.push(`Block ${index + 1}: ungültiges JSON (${error.message})`);
      continue;
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      issues.push(`Block ${index + 1}: JSON-LD muss ein Objekt sein`);
      continue;
    }
    if (!isSchemaOrgContext(data['@context'])) {
      issues.push(`Block ${index + 1}: @context verweist nicht auf Schema.org`);
      continue;
    }
    if (!hasTypedSchemaNode(data)) {
      issues.push(`Block ${index + 1}: kein typisierter @type-Knoten vorhanden`);
      continue;
    }
    schemaOrgBlockCount += 1;
  }

  if (issues.length > 0) return { valid: false, detail: issues.join('; ') };
  return {
    valid: schemaOrgBlockCount > 0,
    detail: `${schemaOrgBlockCount} gültiger Schema.org-JSON-LD-Block${schemaOrgBlockCount === 1 ? '' : 'blöcke'} gefunden.`,
  };
}

function dashboardTarget(rawTarget, fromUrl) {
  try {
    const target = new URL(rawTarget, fromUrl);
    return target.pathname.replace(/\/+$/, '') === '/dashboard';
  } catch {
    return false;
  }
}

function inspectLegacyResponse(response, html, requestUrl) {
  if (response.status >= 300 && response.status < 400) {
    const location = response.headers.get('location');
    const valid = Boolean(location && dashboardTarget(location, requestUrl));
    return {
      valid,
      detail: `HTTP ${response.status}${location ? ` → ${location}` : ' ohne Location-Header'}`,
    };
  }

  if (response.status === 200) {
    const metaTags = [...html.matchAll(/<meta\b([^>]*)>/gi)];
    for (const [, attributes] of metaTags) {
      const httpEquiv = attributes.match(/\bhttp-equiv\s*=\s*(?:"refresh"|'refresh'|refresh(?=\s|$))/i);
      const content = attributes.match(/\bcontent\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i);
      if (!httpEquiv || !content) continue;
      const value = content[1] ?? content[2] ?? content[3] ?? '';
      const target = value.match(/(?:^|;)\s*url\s*=\s*["']?([^"';]+)["']?\s*$/i)?.[1]?.trim();
      if (target && dashboardTarget(target, requestUrl)) {
        return { valid: true, detail: `HTTP 200 mit Meta-Refresh → ${target}` };
      }
    }
    return { valid: false, detail: 'HTTP 200, aber kein Meta-Refresh zur /dashboard/-Route gefunden.' };
  }

  return { valid: false, detail: `Unerwarteter HTTP-Status ${response.status}.` };
}

async function run() {
  console.log(`PuppetNET Produktionsvalidierung: ${baseUrl.origin}`);

  const pageChecks = [
    ['/', 'Startseite'],
    ['/dashboard/', 'Dashboard'],
    ['/sitemap.xml', 'Sitemap'],
    ['/robots.txt', 'robots.txt'],
  ];

  const responses = await Promise.all(pageChecks.map(async ([path, label]) => {
    try {
      const response = await request(path);
      record(`HTTP 200 ${path}`, response.status === 200, `HTTP ${response.status}`);
      return { path, label, response, body: await response.text() };
    } catch (error) {
      record(`HTTP 200 ${path}`, false, describeError(error));
      return { path, label, error };
    }
  }));

  const home = responses.find(({ path }) => path === '/');
  if (home?.body !== undefined && home.response.status === 200) {
    const result = inspectJsonLd(home.body);
    record('Schema.org JSON-LD auf /', result.valid, result.detail);
  } else {
    record('Schema.org JSON-LD auf /', false, 'Startseite konnte nicht erfolgreich geladen werden.');
  }

  const robots = responses.find(({ path }) => path === '/robots.txt');
  const expectedSitemapUrl = new URL('/sitemap.xml', baseUrl.origin).href;
  if (robots?.body !== undefined && robots.response.status === 200) {
    const sitemapEntries = robots.body
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => /^sitemap\s*:/i.test(line))
      .map((line) => line.replace(/^sitemap\s*:\s*/i, '').trim());
    const valid = sitemapEntries.some((entry) => {
      try { return new URL(entry).href === expectedSitemapUrl; } catch { return false; }
    });
    record('Sitemap:-Eintrag in robots.txt', valid,
      valid ? `Eintrag verweist auf ${expectedSitemapUrl}` : `Erwartet: ${expectedSitemapUrl}; gefunden: ${sitemapEntries.join(', ') || 'kein Sitemap-Eintrag'}`);
  } else {
    record('Sitemap:-Eintrag in robots.txt', false, 'robots.txt konnte nicht erfolgreich geladen werden.');
  }

  const legacyUrl = new URL('/legacy', baseUrl.origin);
  try {
    const response = await request('/legacy');
    const body = response.status === 200 ? await response.text() : '';
    const result = inspectLegacyResponse(response, body, legacyUrl);
    record('/legacy leitet nach /dashboard/ weiter', result.valid, result.detail);
  } catch (error) {
    record('/legacy leitet nach /dashboard/ weiter', false, describeError(error));
  }

  const passed = checks.filter(({ passed: ok }) => ok).length;
  for (const check of checks) {
    console.log(`${check.passed ? 'PASS' : 'FAIL'} ${check.label}${check.detail ? ` — ${check.detail}` : ''}`);
  }
  console.log(`\nErgebnis: ${passed}/${checks.length} Prüfungen bestanden.`);
  if (passed !== checks.length) process.exitCode = 1;
}

run().catch((error) => {
  console.error(`Validierung fehlgeschlagen: ${error.stack || error.message}`);
  process.exitCode = 1;
});
