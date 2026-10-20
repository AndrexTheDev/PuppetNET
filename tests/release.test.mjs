import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const read = file => readFileSync(file, 'utf8');
test('SEO generator matches the public entity routes, with absolute deployment URLs', () => {
  const result = spawnSync(process.execPath, ['scripts/generate-seo.js'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const manifest = JSON.parse(read('apps/web/public/data/seo-metadata.json'));
  const sitemap = read('apps/web/public/sitemap.xml');
  assert.ok(Array.isArray(manifest.entities), 'empty authoritative publications are valid');
  for (const entity of manifest.entities) {
    assert.ok(sitemap.includes(`${manifest.origin}/entity/${entity.slug}/`));
    assert.ok(entity.metadata.openGraph);
    assert.ok(entity.schema['@context']);
  }
  assert.ok(read('apps/web/public/robots.txt').includes(`Sitemap: ${manifest.origin}/sitemap.xml`));
  assert.equal(read('apps/web/public/styles/globals.css'), read('apps/web/src/styles/globals.css'));
});
test('invalid origin fails the build before overwriting SEO', () => {
  const result = spawnSync(process.execPath, ['scripts/generate-seo.js'], {
    encoding: 'utf8', env: { ...process.env, SITE_ORIGIN: 'https://example.com/not-an-origin' },
  });
  assert.notEqual(result.status, 0);
});
test('dashboard and legacy colors are token-only; reserved mobile space survives dismissal', () => {
  for (const file of ['apps/web/app/dashboard/dashboard.css', 'apps/web/public/legacy/styles.css']) {
    const css = read(file).replace(/\/\*[\s\S]*?\*\//g, '');
    assert.doesNotMatch(css, /#[0-9a-f]{3,8}\b|rgba?\(/i);
  }
  assert.ok(read('apps/web/app/components/AdContainers.tsx').includes('localStorage.setItem'));
  assert.doesNotMatch(read('apps/web/app/styles.css'), /body:has\(\.mobile-ad-container\)/);
});
test('custom SITE_ORIGIN is used consistently across sitemap and Schema.org', () => {
  try {
    const result = spawnSync(process.execPath, ['scripts/generate-seo.js'], {
      encoding: 'utf8', env: { ...process.env, SITE_ORIGIN: 'https://release.example.org', NEXT_PUBLIC_SITE_ORIGIN: 'https://old.example.org' },
    });
    assert.equal(result.status, 0, result.stderr);
    const sitemap = read('apps/web/public/sitemap.xml');
    const manifest = read('apps/web/public/data/seo-metadata.json');
    assert.ok(sitemap.includes('https://release.example.org/'));
    assert.ok(manifest.includes('https://release.example.org'));
    assert.ok(!manifest.includes('https://old.example.org'));
  } finally {
    const reset = spawnSync(process.execPath, ['scripts/generate-seo.js'], { encoding: 'utf8' });
    assert.equal(reset.status, 0, reset.stderr);
  }
});


test('resource defaults avoid unsolicited GPU renderer and force simulation', () => {
  const dashboard = read('apps/web/app/dashboard/Dashboard.tsx');
  assert.ok(dashboard.includes("useState<'network' | 'map' | 'list'>('list')"));
  assert.ok(dashboard.includes("mode === 'network' && <NetworkGraph"));
  assert.match(read('apps/web/app/dashboard/graph.worker.ts'), /layoutWanted = false/);
  assert.match(read('apps/web/public/legacy/app.js'), /layout: "grid"/);
  const scripts = JSON.parse(read('apps/web/package.json')).scripts;
  assert.ok(scripts.prebuild.includes('generate-seo.js'));
});

test('CSS primitives and canvas palette share the same source', () => {
  const palette = JSON.parse(read('apps/web/src/styles/palette.json'));
  const tokens = read('apps/web/src/styles/globals.css');
  for (const [key, value] of Object.entries(palette)) assert.ok(tokens.includes(`--color-${key}: ${value};`));
  for (const file of ['NetworkGraph.tsx', 'MapModule.tsx', 'graph-data.ts']) {
    assert.doesNotMatch(read(`apps/web/app/dashboard/${file}`), /#[0-9a-f]{6}\b/i);
  }
});


test('empty entity publication has a reserved 404 route and postbuild cleanup', () => {
  const page = read('apps/web/app/entity/[id]/page.tsx');
  assert.ok(page.includes("profiles.length ? profiles : [{ id: '__empty-publication' }]"));
  assert.ok(page.includes('if (!entity) notFound()'));
  const scripts = JSON.parse(read('apps/web/package.json')).scripts;
  assert.match(scripts.postbuild, /finalize-static-export/);
});
