/** Optional real Chromium release check; serve apps/web/out first.
 * SITE_URL=http://127.0.0.1:8080 node tests/release_browser.mjs
 * Requires separately installed Playwright/Chromium; not a runtime dependency.
 */
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { chromium } from 'playwright';
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
  args: process.env.CHROMIUM_ARGS ? JSON.parse(process.env.CHROMIUM_ARGS) : [],
});
const base = process.env.SITE_URL || 'http://127.0.0.1:8080';
const results = [];
mkdirSync('.screenshots/release', { recursive: true });
try {
  for (const width of [320, 375, 768, 1440, 1920]) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.__releaseShifts = [];
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) if (!entry.hadRecentInput) window.__releaseShifts.push(entry.value);
      }).observe({ type: 'layout-shift', buffered: true });
    });
    for (const route of ['/', '/dashboard/']) {
      await page.goto(base + route);
      await page.waitForTimeout(1600);
      const metric = await page.evaluate(() => ({
        overflow: document.documentElement.scrollWidth > innerWidth,
        shiftSum: window.__releaseShifts.reduce((sum, value) => sum + value, 0),
      }));
      assert.equal(metric.overflow, false, `${route} at ${width}: horizontal overflow`);
      // Sum across this short load is stricter than the standard CLS session-window score.
      assert.ok(metric.shiftSum <= 0.1, `${route} at ${width}: layout shift sum ${metric.shiftSum}`);
      results.push({ width, route, ...metric });
      await page.screenshot({ path: `.screenshots/release/${width}-${route === '/' ? 'home' : route.split('/')[1]}.png` });
    }
    // The archived console path must land on the dashboard — even without the
    // HTTP 301 a Pages deployment serves, the meta refresh has to forward.
    await page.goto(base + '/legacy/');
    await page.waitForURL('**/dashboard/', { timeout: 15000 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: `.screenshots/release/${width}-legacy-redirect.png` });
    if (width < 1100) {
      await page.goto(base + '/');
      const padding = await page.locator('body').evaluate(el => getComputedStyle(el).paddingBottom);
      await page.getByRole('button', { name: 'Werbeplatz schließen' }).click();
      assert.equal(await page.locator('body').evaluate(el => getComputedStyle(el).paddingBottom), padding);
      await page.reload();
      await page.waitForTimeout(500);
      assert.equal(await page.locator('.mobile-ad-container').count(), 0, 'dismissal persists after reload');
      assert.equal(await page.locator('body').evaluate(el => getComputedStyle(el).paddingBottom), padding);
    } else {
      await page.goto(base + '/');
      assert.equal(await page.locator('.ad-header').evaluate(el => el.getBoundingClientRect().height), 90);
      if (width >= 1600) {
        assert.deepEqual(await page.locator('.ad-left').evaluate(el => ({ width: el.offsetWidth, height: el.offsetHeight })), { width: 160, height: 600 });
      }
    }
    assert.deepEqual(errors, []);
    await context.close();
  }
  const context = await browser.newContext({ viewport: { width: 375, height: 900 }, reducedMotion: 'reduce' });
  await context.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('Blocked', 'SecurityError'); } });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/');
  await page.getByRole('button', { name: 'Werbeplatz schließen' }).click();
  assert.equal(await page.locator('.mobile-ad-container').count(), 0);
  assert.deepEqual(errors, []);
  await context.close();
  console.log('PASS blocked storage: banner still closes without uncaught errors');
} finally {
  await browser.close();
  writeFileSync('.screenshots/release/results.json', JSON.stringify(results, null, 2));
}
console.log(results);
