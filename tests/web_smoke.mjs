#!/usr/bin/env node
/**
 * web_smoke.mjs — source-contract smoke test for the modern static web app.
 *
 *   node tests/web_smoke.mjs            (npm run test:web)
 *   run by scripts/pages-build.mjs      (Cloudflare Pages build gate)
 *
 * The retained v1 analyst console under apps/web/public/legacy is archived;
 * nothing in this suite boots a browser any more. What it pins is the contract
 * of the modern Next.js app *before* `next build` runs, because the build gate
 * needs failures that are cheap, deterministic and independent of webpack:
 *
 *   1. the legacy console is really gone, and nothing links to /legacy;
 *   2. /legacy is a permanent static redirect to /dashboard/ (route + Pages
 *      _redirects), never an indexed document;
 *   3. Cloudflare response headers cache the export the way the deployment
 *      depends on (hashed assets immutable, documents revalidated, redirect
 *      answers never cached long);
 *   4. crawler directives and the SEO emitter only ever list active App Router
 *      documents — the landing page, the dashboard shell and public profiles;
 *   5. the published-data contract (graph_latest.json path) is unchanged.
 *
 * Behavioural coverage lives next door: dashboard.test.ts, data_fetcher.test.ts,
 * seo.test.ts and a11y.test.tsx run pre-build, tests/export_smoke.mjs asserts
 * the built apps/web/out afterwards.
 */
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PUBLIC = path.join(ROOT, "apps/web", "public");
const APP = path.join(ROOT, "apps/web", "app");
const read = (...parts) => readFileSync(path.join(...parts), "utf8");

let checks = 0;
function check(name, fn) {
  checks += 1;
  try {
    fn();
    console.log(`ok    ${name}`);
  } catch (error) {
    console.error(`FAIL  ${name}\n      ${error.message.split("\n")[0]}`);
    process.exitCode = 1;
  }
}

/* -------------------------------------------------------------------------- */
/*  1. The legacy console is archived                                          */
/* -------------------------------------------------------------------------- */

check("the legacy console directory and its compatibility symlink are gone", () => {
  assert.equal(existsSync(path.join(PUBLIC, "legacy")), false,
    "apps/web/public/legacy exists: the v1 console must not ship in parallel");
  assert.equal(existsSync(path.join(ROOT, "web")), false,
    "the root web/ path exists: it pointed at the legacy console");
});

check("no modern page or component links to /legacy any more", () => {
  for (const file of ["app/page.tsx", "app/dashboard/Dashboard.tsx"]) {
    const source = read(ROOT, "apps/web", file);
    assert.doesNotMatch(source, /["']\/legacy/, `${file} still links to /legacy`);
  }
});

/* -------------------------------------------------------------------------- */
/*  2. /legacy is a permanent static redirect                                  */
/* -------------------------------------------------------------------------- */

check("the /legacy route permanently redirects to the dashboard", () => {
  const source = read(APP, "legacy", "page.tsx");
  // A static export cannot use next.config redirects() (ignored with output:
  // 'export'), and redirect() needs JavaScript — so the page renders the
  // zero-delay meta refresh itself. React 19 hoists the <meta> into <head>.
  assert.match(source, /httpEquiv="refresh"/, "app/legacy/page.tsx must render a meta refresh");
  assert.match(source, /content="0; url=\/dashboard\/"/,
    "the refresh must be immediate and target /dashboard/");
  assert.match(source, /href="\/dashboard\/"/, "a visible fallback link must exist");
  assert.match(source, /index:\s*false/, "the redirect page must not be indexed");
  assert.match(source, /canonical:\s*['"]\/dashboard\/['"]/,
    "the redirect page must canonically point at /dashboard/");
});

check("Cloudflare Pages serves a real 301 for every former /legacy path", () => {
  const redirects = read(PUBLIC, "_redirects");
  assert.match(redirects, /^\/legacy\s+\/dashboard\/\s+301$/m, "exact /legacy rule");
  assert.match(redirects, /^\/legacy\/\*\s+\/dashboard\/\s+301$/m,
    "splat rule for deep links like /legacy/index.html");
});

check("the dashboard link in the export resolves (trailing slash is canonical)", () => {
  const config = read(ROOT, "apps/web", "next.config.mjs");
  assert.match(config, /trailingSlash:\s*true/, "/dashboard/ is the canonical form");
  assert.match(config, /output:\s*['"]export['"]/, "the app stays a static export");
});

/* -------------------------------------------------------------------------- */
/*  3. Response headers                                                        */
/* -------------------------------------------------------------------------- */

check("deployment headers keep the security baseline and the cache contract", () => {
  const headers = read(PUBLIC, "_headers");
  assert.match(headers, /^\/\*\n(?:[ \t]+.*\n)*?[ \t]+X-Content-Type-Options: nosniff/m);
  assert.match(headers, /Content-Security-Policy: frame-ancestors 'self'/);
  assert.match(headers, /\/_next\/static\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: public, max-age=31536000, immutable/,
    "hashed Next assets must be cached immutable");
  assert.match(headers, /\/dashboard\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: no-cache/,
    "dashboard HTML must be revalidated");
  assert.match(headers, /\/entity\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: no-cache/,
    "profile HTML must be revalidated");
  assert.match(headers, /\/data\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: public, max-age=60/,
    "published graph data is short-cached, not immutable");
  assert.match(headers, /\/legacy\/\*\n(?:[ \t]+.*\n)*?[ \t]+Cache-Control: no-cache/,
    "redirect answers must not be cached long");
  assert.doesNotMatch(headers, /\/legacy\/vendor|X-XSS-Protection/,
    "legacy console header blocks must not survive the archive");
});

/* -------------------------------------------------------------------------- */
/*  4. Crawler directives and sitemap coupling                                 */
/* -------------------------------------------------------------------------- */

check("robots.txt excludes the redirect and names exactly one sitemap", () => {
  const robots = read(PUBLIC, "robots.txt");
  assert.match(robots, /^User-agent:\s*\*$/m);
  assert.match(robots, /^Disallow:\s*\/legacy$/m, "the redirect path is not crawlable content");
  assert.doesNotMatch(robots, /\/legacy\/sitemap\.xml/, "no legacy sitemap reference");
  const sitemapLines = robots.match(/^Sitemap:.*$/gm) ?? [];
  assert.equal(sitemapLines.length, 1, `exactly one Sitemap line, got ${sitemapLines.length}`);
});

check("the SEO emitter only indexes active App Router documents", () => {
  const generator = read(ROOT, "scripts", "generate-seo.js");
  assert.match(generator, /\{\s*loc:\s*['"]\/['"]/);
  assert.match(generator, /\{\s*loc:\s*['"]\/dashboard\/['"]/);
  assert.doesNotMatch(generator, /loc:\s*['"]\/legacy(\/)?['"]/,
    "generate-seo.js must not emit /legacy entries");
  const emitter = read(ROOT, "scripts", "emit-seo-files.mjs");
  assert.doesNotMatch(emitter, /loc:\s*["']\/legacy(\/)?["']/,
    "emit-seo-files.mjs must not emit /legacy entries");
  // The build gate wires the generator in before next build.
  assert.match(read(ROOT, "scripts", "pages-build.mjs"), /generate-seo\.js/);
});

check("a generated sitemap lists no /legacy path", () => {
  const sitemap = path.join(PUBLIC, "sitemap.xml");
  if (!existsSync(sitemap)) return; // generated at build time; release.test.mjs covers it
  const xml = readFileSync(sitemap, "utf8");
  assert.doesNotMatch(xml, /\/legacy/, "sitemap.xml must not contain /legacy URLs");
  assert.match(xml, /\/dashboard\//);
});

/* -------------------------------------------------------------------------- */
/*  5. Published-data contract                                                 */
/* -------------------------------------------------------------------------- */

check("the dashboard reads the reviewed static snapshot from the canonical path", () => {
  const fetcher = read(ROOT, "apps/web", "src", "lib", "dataFetcher.ts");
  assert.match(fetcher, /STATIC_GRAPH_PATH = '\/data\/graph_latest\.json'/);
  // An unusable publication is standby, not a silent demo swap.
  assert.match(fetcher, /source: 'standby'/, "loadStaticGraph must expose a standby result");
  const worker = read(ROOT, "apps", "worker", "src", "index.ts");
  assert.match(worker, /graph_latest\.json/, "the search relay serves the same aggregate");
});

check("the build works from a clean checkout: the demo dataset is committed", () => {
  const sample = JSON.parse(read(PUBLIC, "data", "entities.sample.json"));
  assert.ok(Array.isArray(sample.entities) && sample.entities.length >= 8,
    "entities.sample.json must carry the fictional demo profiles");
});

console.log(`\nweb smoke test: ${checks} source-contract checks ${process.exitCode ? "FAILED" : "passed"}`);
