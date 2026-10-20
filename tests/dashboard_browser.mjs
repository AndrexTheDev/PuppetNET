/** Optional real-browser acceptance. Serve apps/web/out first. Never calls live tiles/APIs.
 * npm install --no-save playwright && npx playwright install chromium
 * DASHBOARD_URL=http://127.0.0.1:8080/dashboard/ node tests/dashboard_browser.mjs
 */
import assert from 'node:assert/strict';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
  args: process.env.CHROMIUM_ARGS ? JSON.parse(process.env.CHROMIUM_ARGS) : [],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
const errors = [], requests = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => requests.push(request.url()));
const url = process.env.DASHBOARD_URL || 'http://127.0.0.1:8080/dashboard/';
const host = page.locator('[data-testid=sigma-host]');
async function idle() {
  await page.locator('.dashboard-shell[aria-busy=false]').waitFor({ timeout: 60_000 });
  await page.locator('.stage-loading').waitFor({ state: 'hidden', timeout: 60_000 });
}
try {
  await page.goto(url);
  await idle();
  await page.getByRole('button', { name: '◈ Netzwerk', exact: true }).click();
  await page.getByRole('button', { name: 'Layout', exact: true }).click();
  assert.equal(await host.getAttribute('data-rendered-nodes'), '900');
  assert.equal(await host.locator('canvas').count(), 1);
  await page.waitForFunction(() => Number(document.querySelector('[data-testid=sigma-host]')?.getAttribute('data-layout-updates')) > 0);
  assert.equal(requests.filter(url => url.includes('tile.openstreetmap')).length, 0);
  console.log('PASS: WebGL2 canvas, real worker position updates, no unsolicited tile requests');

  await page.getByRole('button', { name: '☷ Tabelle', exact: true }).click();
  await page.locator('tbody button').first().click();
  await page.getByRole('heading', { name: 'Quellnachweise' }).waitFor();
  assert.match(await page.locator('.evidence-list').innerText(), /Fiktiver Beispieldatensatz/);
  await page.getByRole('button', { name: '◈ Netzwerk', exact: true }).click();
  assert.equal(errors.length, 0, errors.join('\n'));
  console.log('PASS: node inspector, evidence, hidden-canvas resize without errors');

  const start = performance.now();
  await page.getByRole('button', { name: '120.000 Elemente testen', exact: true }).click();
  await page.getByText('Fiktiver Lasttest · 120.000 Elemente', { exact: true }).waitFor({ timeout: 60_000 });
  await idle();
  assert.equal(await host.getAttribute('data-rendered-nodes'), '24');
  await page.getByRole('button', { name: 'Vergrößern', exact: true }).click();
  // Wait for the camera's animation rather than firing both clicks into the same frame.
  await page.waitForTimeout(300);
  await page.getByRole('button', { name: 'Vergrößern', exact: true }).click();
  await page.getByText('Gruppen', { exact: true }).waitFor();
  await idle();
  assert.equal(await host.getAttribute('data-rendered-nodes'), '96');
  console.log(`PASS: 120000 elements, adaptive LoD 24 -> 96 groups; elapsed ${(performance.now() - start).toFixed(0)} ms`);

  await page.getByRole('button', { name: '☷ Tabelle', exact: true }).click();
  await page.locator('tbody button').first().click();
  await page.getByRole('button', { name: 'Gruppe untersuchen' }).click();
  await idle();
  await page.getByRole('button', { name: '← Gesamten Ausschnitt anzeigen' }).waitFor();
  assert.ok(Number(await host.getAttribute('data-rendered-nodes')) < 5000);
  await page.getByRole('button', { name: '← Gesamten Ausschnitt anzeigen' }).click();
  await idle();
  await page.getByRole('button', { name: '◈ Netzwerk', exact: true }).click();
  page.once('dialog', dialog => dialog.accept());
  await page.getByLabel('Adaptives Detailniveau', { exact: true }).uncheck();
  await page.waitForFunction(() => document.querySelector('[data-testid=sigma-host]')?.getAttribute('data-rendered-nodes') === '30000', { timeout: 60_000 });
  await idle();
  assert.equal(await host.getAttribute('data-rendered-nodes'), '30000');
  assert.equal(await host.getAttribute('data-rendered-edges'), '90000');
  const frames = await page.evaluate(() => new Promise(resolve => {
    let count = 0; const end = performance.now() + 1000;
    const tick = () => { count++; if (performance.now() < end) requestAnimationFrame(tick); else resolve(count); }; requestAnimationFrame(tick);
  }));
  console.log(`PASS: raw 30000 nodes / 90000 edges handed to Sigma; ${frames} animation callbacks over ~1s (environment-specific, NOT a hardware FPS guarantee)`);
  await page.getByLabel('Adaptives Detailniveau', { exact: true }).check();
  await page.waitForFunction(() => Number(document.querySelector('[data-testid=sigma-host]')?.getAttribute('data-rendered-nodes')) < 5000); await idle();

  await page.getByRole('button', { name: '◎ Karte', exact: true }).click();
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==', 'base64') }));
  await page.getByRole('button', { name: 'OpenStreetMap-Karte laden', exact: true }).click();
  await page.locator('.leaflet-tile-loaded').first().waitFor();
  await page.locator('.leaflet-overlay-pane canvas').waitFor();
  assert.ok(requests.some(url => url.includes('tile.openstreetmap.org/')));
  assert.match(await page.locator('.leaflet-control-attribution').innerText(), /OpenStreetMap/);
  console.log('PASS: Leaflet canvas markers, opt-in tiles (stubbed), visible attribution');

  const fixture = { nodes: [{ id: 'person:hostile', name: '<img src=x onerror=alert(1)>', type: 'Person', confidence: .9 }, { id: 'doc', type: 'Document', title: '<script>alert(1)</script>', url: 'javascript:alert(1)', confidence: 1 }], edges: [{ source: 'person:hostile', target: 'doc', type: 'EVIDENCED_BY', confidence: .9, weight: .9 }] };
  await page.locator('input[type=file]').setInputFiles({ name: 'evidence.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(fixture)) }); await idle();
  await page.getByRole('button', { name: '☷ Tabelle', exact: true }).click();
  await page.locator('tbody button').first().click();
  await page.getByText('<script>alert(1)</script>', { exact: true }).waitFor();
  assert.equal(await page.locator('.evidence-list a').count(), 0);
  assert.equal(await page.locator('.node-title img').count(), 0);
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Abfrage ↓', exact: true }).click();
  const download = await downloadPromise;
  assert.equal(download.suggestedFilename(), 'puppetnet-query.cypher');
  await page.locator('input[type=file]').setInputFiles({ name: 'invalid.json', mimeType: 'application/json', buffer: Buffer.from('{}') });
  await page.locator('.dashboard-alert[role=alert]').waitFor();
  assert.match(await page.locator('.source-strip strong').innerText(), /evidence.json/);
  assert.equal(await page.evaluate(() => localStorage.length), 0);
  console.log('PASS: EVIDENCED_BY import, hostile text escaping, unsafe link rejection, Cypher download, honest failed import');

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '◈ Netzwerk', exact: true }).click();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  assert.deepEqual(errors, []);
  console.log('PASS: mobile width and zero uncaught browser errors');
  const fallback = await browser.newPage();
  fallback.on('pageerror', error => errors.push(error.message));
  await fallback.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...args) { return type === 'webgl2' ? null : original.call(this, type, ...args); };
  });
  await fallback.goto(url);
  await fallback.locator('.stage-error').filter({ hasText: 'WebGL 2 ist nicht verfügbar' }).waitFor();
  await fallback.getByRole('button', { name: '☷ Tabelle', exact: true }).click();
  await fallback.locator('tbody button').first().waitFor();
  assert.deepEqual(errors, []);
  await fallback.close();
  console.log('PASS: no-WebGL fallback preserves accessible data table');
} finally { await browser.close(); }
