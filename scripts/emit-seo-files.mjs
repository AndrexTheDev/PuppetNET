#!/usr/bin/env node
/**
 * emit-seo-files.mjs — write the SEO artefacts that need a hostname.
 *
 *   npm run emit:seo -- --origin https://puppetnet-console.pages.dev
 *   SITE_ORIGIN=https://console.example node scripts/emit-seo-files.mjs
 *   node scripts/emit-seo-files.mjs --build-gate      # run by scripts/pages-build.mjs
 *
 * A `Sitemap:` line and every `<loc>` in a sitemap must be absolute, and this
 * repository is committed long before anybody knows which domain will serve it.
 * Guessing a hostname in a committed file would be worse than shipping none: a
 * sitemap that points at somebody else's domain is a sitemap that teaches
 * crawlers to go elsewhere. So the files that need an origin are generated at
 * build/deploy time from the same variable that names the project — and both
 * sitemaps are git-ignored.
 *
 * The `robots.txt` files *are* committed, because their directives are
 * origin-free and a deployment from a USB stick should still have them. This
 * script rewrites only their `Sitemap:` line, in place and idempotently, so the
 * committed file stays the single source of truth for everything else.
 *
 * Two targets, because v2 ships two frontends from one Pages project:
 *   apps/web/public/  — the Next.js 15 static export root (`/`, `/dashboard/`)
 *   web/ (→ apps/web/public/legacy/) — the retained v1 analyst console
 *
 * Dependency-free on purpose: the build gate runs it without extra installs.
 */

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const TARGETS = [
  {
    name: "static Next export",
    robots: "apps/web/public/robots.txt",
    sitemap: "apps/web/public/sitemap.xml",
    // Where this sitemap is served from: it is the one the root robots.txt names.
    servedAt: "/sitemap.xml",
    history: "apps/web/app/dashboard/page.tsx",
    // `trailingSlash: true` in next.config.mjs makes /dashboard/ the canonical
    // form; a sitemap that lists /dashboard would redirect to itself.
    urls: [{ loc: "/", priority: "1.0" }, { loc: "/dashboard/", priority: "0.9" }],
    note: "Two URLs, because the v2 export serves two documents: the landing page and\n"
      + "     the WebGL dashboard. Every graph it shows is client-side state — an\n"
      + "     import, a fictional fixture or a token-gated API response — never a\n"
      + "     crawlable document.",
  },
  {
    name: "legacy console",
    robots: "web/robots.txt",
    sitemap: "web/sitemap.xml",
    // v1 served the console from `/`; v2 retains it under `/legacy/`, so both
    // the Sitemap line and the `<loc>` have to name the path it lives at now.
    servedAt: "/legacy/sitemap.xml",
    history: "web/index.html",
    urls: [{ loc: "/legacy/", priority: "0.6" }],
    note: "One URL, because the console is a single-page application: every view is a\n"
      + "     hash fragment of \"/legacy/\", and fragments are not separate documents.",
  },
];

function fail(message) {
  console.error(`emit-seo: ${message}`);
  process.exit(1);
}

/* ---- origin ------------------------------------------------------------- */

const argv = process.argv.slice(2);
// Inside the Pages build gate an unusable SITE_ORIGIN must not take the whole
// deployment down: the site is still correct, only its sitemap is missing. On
// the command line the same value is a typo the operator has to see.
const buildGate = argv.includes("--build-gate");
const flagIndex = argv.indexOf("--origin");
const rawOrigin = (flagIndex >= 0 ? argv[flagIndex + 1] : null) || process.env.SITE_ORIGIN || "";

if (!rawOrigin) {
  // Not an error: a local build or a deploy without a known domain simply keeps
  // the origin-free robots.txt files and ships no sitemap.
  console.log("emit-seo: no --origin / SITE_ORIGIN given — leaving the committed robots.txt files "
    + "as they are and writing no sitemap. Pass the deployment origin to emit both.");
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
  const message = `"${rawOrigin}" is not a usable deployment origin (${error.message}). `
    + "Expected something like https://puppetnet-console.pages.dev";
  if (buildGate) {
    console.warn(`emit-seo: ${message} — continuing the build without SEO artefacts.`);
    process.exit(0);
  }
  fail(message);
}

/* ---- lastmod: the real edit date of the frontend, not "today" ----------- */

/** A checkout stamps every file with the clone time, so mtime would claim the
 *  frontend changed on every deploy. Git knows better; fall back if it cannot. */
function lastModified(relative) {
  try {
    const date = execFileSync("git", ["log", "-1", "--format=%cs", "--", relative], {
      cwd: ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(date)) return date;
  } catch (_) { /* no git history (a tarball, a shallow fetch) — fall through */ }
  return new Date().toISOString().slice(0, 10);
}

/* ---- sitemap.xml -------------------------------------------------------- */

const written = [];
for (const target of TARGETS) {
  const robotsPath = path.join(ROOT, target.robots);
  const sitemapPath = path.join(ROOT, target.sitemap);
  if (!existsSync(robotsPath)) {
    fail(`${target.robots} is missing — it is committed, so restore it first.`);
  }

  const lastmod = lastModified(target.history);
  const urls = target.urls
    .map((url) => `  <url>
    <loc>${origin}${url.loc}</loc>
    <lastmod>${lastmod}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>${url.priority}</priority>
  </url>`)
    .join("\n");
  writeFileSync(sitemapPath, `<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated at build time by scripts/emit-seo-files.mjs from the deployment
     origin; git-ignored, never committed. ${target.name}.
     ${target.note} -->
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls}
</urlset>
`, "utf8");

  /* ---- robots.txt: replace only the Sitemap line ------------------------ */
  const committed = readFileSync(robotsPath, "utf8");
  const withoutSitemap = committed
    .split("\n")
    .filter((line) => !/^sitemap:/i.test(line.trim()))
    .join("\n")
    .replace(/\n+$/, "");
  writeFileSync(robotsPath, `${withoutSitemap}\n\nSitemap: ${origin}${target.servedAt}\n`, "utf8");

  written.push(`${target.sitemap} (${target.urls.length} URL(s), served at ${target.servedAt})`);
}

console.log(`emit-seo: origin ${origin} — wrote ${written.join("; ")} and rewrote each Sitemap line`);
