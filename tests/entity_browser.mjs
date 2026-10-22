/** Optional Chromium acceptance for static entity profile pages.
 * Serve apps/web/out, then SITE_URL=http://127.0.0.1:8080 node tests/entity_browser.mjs
 * Regression (beta 2026-10-21): the global `header{display:flex}` squeezed the
 * profile title into a narrow column and broke names letter by letter
 * ("Nord|hafen|Logis|tik AG"). Every word of the title must stay on one line.
 */
import assert from 'node:assert/strict';
import {chromium} from 'playwright';

const base = process.env.SITE_URL || 'http://127.0.0.1:8080';
const browser = await chromium.launch({headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? {executablePath: process.env.CHROMIUM_EXECUTABLE} : {}),
  args: process.env.CHROMIUM_ARGS ? JSON.parse(process.env.CHROMIUM_ARGS) : []});
const results = [];
try {
  const index = await browser.newPage();
  await index.goto(base + '/sitemap.xml');
  const xml = await index.evaluate(() => document.documentElement.outerHTML);
  const paths = [...new Set([...xml.matchAll(/https?:\/\/[^<\s]+?(\/entity\/[^<\s/]+\/)/g)].map(m => m[1]))];
  assert.ok(paths.length > 0, 'sitemap lists entity pages');
  // the three longest slugs are the hardest titles
  const pick = paths.sort((a, b) => b.length - a.length).slice(0, 3);
  await index.close();
  for (const width of [360, 820, 1280]) {
    for (const path of pick) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      const response = await page.goto(base + path);
      assert.equal(response.status(), 200, path);
      const m = await page.evaluate(() => {
        const header = document.querySelector('.entity-header');
        const h1 = header.querySelector('h1');
        const text = h1.firstChild;
        const broken = [];
        const re = /\S+/g;
        let match;
        while ((match = re.exec(text.textContent))) {
          const range = document.createRange();
          range.setStart(text, match.index);
          range.setEnd(text, match.index + match[0].length);
          const tops = new Set([...range.getClientRects()].map(r => Math.round(r.top)));
          if (tops.size > 1 && match[0].length <= 18) broken.push(match[0]);
        }
        return {display: getComputedStyle(header).display, broken, name: text.textContent,
          overflow: document.documentElement.scrollWidth > innerWidth};
      });
      assert.equal(m.display, 'block', `${path} @${width}: header must not inherit the site-header flex row`);
      assert.deepEqual(m.broken, [], `${path} @${width}: "${m.name}" breaks inside words`);
      assert.equal(m.overflow, false, `${path} @${width}: horizontal overflow`);
      assert.deepEqual(errors, [], `${path} @${width}: JS errors`);
      results.push({width, path, name: m.name, passed: true});
      await page.close();
    }
  }
} finally {
  await browser.close();
}
console.log(results);
console.log(`ENTITY BROWSER: ${results.length} page checks passed`);
