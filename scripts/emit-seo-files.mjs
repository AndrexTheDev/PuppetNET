#!/usr/bin/env node
/**
 * emit-seo-files.mjs — write the two SEO artefacts that need a hostname.
 *
 *   npm run emit:seo -- --origin https://puppetnet-console.pages.dev
 *   SITE_ORIGIN=https://console.example node scripts/emit-seo-files.mjs
 *
 * A `Sitemap:` line and every `<loc>` in a sitemap must be absolute, and this
 * repository is committed long before anybody knows which domain will serve it.
 * Guessing a hostname in a committed file would be worse than shipping none: a
 * sitemap that points at somebody else's domain is a sitemap that teaches
 * crawlers to go elsewhere. So the files that need an origin are generated at
 * deploy time, by the Pages workflow, from the same variable that names the
 * project — and `web/sitemap.xml` is git-ignored.
 *
 * `web/robots.txt` *is* committed, because its directives are origin-free and a
 * deployment from a USB stick should still have them. This script rewrites only
 * its `Sitemap:` line, in place and idempotently, so the committed file stays the
 * single source of truth for everything else.
 *
 * Dependency-free on purpose: the deploy job runs it without `npm ci`.
 */

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ROBOTS = path.join(ROOT, "web", "robots.txt");
const SITEMAP = path.join(ROOT, "web", "sitemap.xml");

function fail(message) {
  console.error(`emit-seo: ${message}`);
  process.exit(1);
}

/* ---- origin ------------------------------------------------------------- */

const argv = process.argv.slice(2);
const flagIndex = argv.indexOf("--origin");
const rawOrigin = (flagIndex >= 0 ? argv[flagIndex + 1] : null) || process.env.SITE_ORIGIN || "";

if (!rawOrigin) {
  // Not an error: a local build or a deploy without a known domain simply keeps
  // the origin-free robots.txt and ships no sitemap.
  console.log("emit-seo: no --origin / SITE_ORIGIN given — leaving web/robots.txt as committed "
    + "and writing no sitemap. Pass the deployment origin to emit both.");
  process.exit(0);
}

let origin;
try {
  const url = new URL(rawOrigin);
  if (!/^https?:$/.test(url.protocol)) throw new Error("not http(s)");
  if (url.pathname !== "/" && url.pathname !== "") throw new Error("has a path");
  if (url.search || url.hash) throw new Error("has a query or fragment");
  origin = url.origin;
} catch (error) {
  fail(`"${rawOrigin}" is not a usable deployment origin (${error.message}). `
    + "Expected something like https://puppetnet-console.pages.dev");
}

/* ---- lastmod: the real edit date of the console, not "today" ------------ */

/** A checkout stamps every file with the clone time, so mtime would claim the
 *  console changed on every deploy. Git knows better; fall back if it cannot. */
function lastModified() {
  try {
    const date = execFileSync("git", ["log", "-1", "--format=%cs", "--", "web/index.html"], {
      cwd: ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(date)) return date;
  } catch (_) { /* no git history (a tarball, a shallow fetch) — fall through */ }
  return new Date().toISOString().slice(0, 10);
}

/* ---- sitemap.xml -------------------------------------------------------- */

const sitemap = `<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated at deploy time by scripts/emit-seo-files.mjs from the deployment
     origin. One URL, because the console is a single-page application: every
     view is a hash fragment of "/", and fragments are not separate documents. -->
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>${origin}/</loc>
    <lastmod>${lastModified()}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
`;
writeFileSync(SITEMAP, sitemap, "utf8");

/* ---- robots.txt: replace only the Sitemap line -------------------------- */

if (!existsSync(ROBOTS)) fail("web/robots.txt is missing — it is committed, so restore it first.");
const committed = readFileSync(ROBOTS, "utf8");
const withoutSitemap = committed
  .split("\n")
  .filter((line) => !/^sitemap:/i.test(line.trim()))
  .join("\n")
  .replace(/\n+$/, "");
writeFileSync(ROBOTS, `${withoutSitemap}\n\nSitemap: ${origin}/sitemap.xml\n`, "utf8");

console.log(`emit-seo: wrote web/sitemap.xml and pointed web/robots.txt at ${origin}/sitemap.xml`);
