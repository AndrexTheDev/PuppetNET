#!/usr/bin/env node
/**
 * Static SEO preparation for every `next build` (also direct invocations).
 * HTML OG tags / JSON-LD are rendered by the entity route using the same helpers.
 * This script emits the matching sitemap, robots and inspectable SEO manifest.
 * No network access, runtime API, database, model or secret is needed.
 */
import { spawnSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, readdirSync, unlinkSync, copyFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { entityEntries, normalizeOrigin, renderSitemaps, writeSitemaps } from './generate-sitemap.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const origin = normalizeOrigin(process.env.SITE_ORIGIN || process.env.NEXT_PUBLIC_SITE_ORIGIN || 'https://puppetnet-web.pages.dev');
const env = { ...process.env, SITE_ORIGIN: origin, NEXT_PUBLIC_SITE_ORIGIN: origin };
const result = spawnSync(process.execPath, ['--import', 'tsx', 'scripts/generate-seo-data.ts'], {
  cwd: root, env, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
});
if (result.status !== 0) throw new Error(`SEO dataset failed: ${result.stderr || result.error || result.status}`);
const dataset = JSON.parse(result.stdout);
if (dataset.origin !== origin) throw new Error('SEO origin differs from entity metadata origin');
const publicDir = path.join(root, 'apps/web/public');
const profiles = entityEntries(dataset.entities, dataset.generatedAt);
const entries = [
  { loc: '/', priority: '1.0', changefreq: 'weekly' },
  { loc: '/dashboard/', priority: '0.9', changefreq: 'monthly' },
  { loc: '/legacy/', priority: '0.6', changefreq: 'monthly' },
  ...profiles,
];
const { files, urlCount } = renderSitemaps({ origin, entries, lastmod: dataset.generatedAt });
// Remove obsolete sitemap shards when the publication shrinks.
for (const name of readdirSync(publicDir)) {
  if (/^sitemap(?:-\d+)?\.xml$/.test(name)) unlinkSync(path.join(publicDir, name));
}
writeSitemaps(publicDir, files);
function robots(file, sitemap) {
  const text = readFileSync(file, 'utf8').split('\n')
    .filter(line => !/^sitemap:/i.test(line.trim())).join('\n').trimEnd();
  writeFileSync(file, `${text}\n\nSitemap: ${origin}${sitemap}\n`);
}
robots(path.join(publicDir, 'robots.txt'), '/sitemap.xml');
const legacyDir = path.join(publicDir, 'legacy');
writeSitemaps(legacyDir, renderSitemaps({ origin, entries: [{ loc: '/legacy/' }], lastmod: dataset.generatedAt }).files);
robots(path.join(legacyDir, 'robots.txt'), '/legacy/sitemap.xml');
mkdirSync(path.join(publicDir, 'data'), { recursive: true });
writeFileSync(path.join(publicDir, 'data/seo-metadata.json'), `${JSON.stringify(dataset, null, 2)}\n`);
// One palette source for CSS, WebGL and Leaflet. Keep the CSS mirror deterministic.
const tokenPath = path.join(root, 'apps/web/src/styles/globals.css');
const palette = JSON.parse(readFileSync(path.join(root, 'apps/web/src/styles/palette.json'), 'utf8'));
const tokens = readFileSync(tokenPath, 'utf8').replace(/--color-([\w-]+):\s*#[0-9a-f]{6}/g, (match, key) => {
  if (!/^#[0-9a-f]{6}$/i.test(palette[key] || '')) throw new Error(`Invalid palette token: ${key}`);
  return `--color-${key}: ${palette[key]}`;
});
if (tokens !== readFileSync(tokenPath, 'utf8')) writeFileSync(tokenPath, tokens);
mkdirSync(path.join(publicDir, 'styles'), { recursive: true });
copyFileSync(path.join(root, 'apps/web/src/styles/globals.css'), path.join(publicDir, 'styles/globals.css'));
const og = spawnSync(process.execPath, ['scripts/emit-og-images.mjs'], { cwd: root, env, stdio: 'inherit' });
if (og.status !== 0) throw new Error('Open Graph image generation failed');
console.log(`generate-seo: ${origin} — ${urlCount} URLs, ${profiles.length} entity metadata records`);
