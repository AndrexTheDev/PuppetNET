/** Beta S11 browser half: serve a built apps/web/out that contains a pipeline
 * export and check that the dashboard and an entity page show exactly the
 * released data.  Usage: node scripts/beta/crossstack_browser.mjs <outDir>
 * Needs Playwright + Chromium (CHROMIUM_EXECUTABLE / CHROMIUM_ARGS honoured).
 */
import {createServer} from 'node:http';
import {readFileSync, existsSync, statSync, readdirSync} from 'node:fs';
import {join, extname, normalize} from 'node:path';
import {chromium} from 'playwright';

const root = process.argv[2];
const types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.json': 'application/json',
  '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.webmanifest': 'application/manifest+json',
  '.txt': 'text/plain', '.xml': 'application/xml', '.woff2': 'font/woff2'};
const server = createServer((req, res) => {
  let path = normalize(decodeURIComponent(new URL(req.url, 'http://x').pathname)).replace(/^(\.\.[/\\])+/, '');
  let file = join(root, path);
  if (existsSync(file) && statSync(file).isDirectory()) file = join(file, 'index.html');
  if (!existsSync(file)) { res.writeHead(404); res.end('not found'); return; }
  res.writeHead(200, {'content-type': types[extname(file)] || 'application/octet-stream'});
  res.end(readFileSync(file));
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE ? {executablePath: process.env.CHROMIUM_EXECUTABLE} : {}),
  args: process.env.CHROMIUM_ARGS ? JSON.parse(process.env.CHROMIUM_ARGS) : []});
const check = (name, ok, detail = '') => console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`);
try {
  const page = await browser.newPage({viewport: {width: 1280, height: 900}});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(() => sessionStorage.setItem('puppetnet-intro-seen', '1'));
  await page.goto(base + '/dashboard/');
  await page.waitForFunction(() => document.body.innerText.includes('Nordhafen Logistik AG')
    || document.querySelector('.standby'), null, {timeout: 60000}).catch(() => {});
  const text = await page.evaluate(() => document.body.innerText);
  check('S11 dashboard boots with the live export (no standby, no demo)',
    !(await page.locator('.standby').count()) && text.includes('Nordhafen Logistik AG'));
  check('S11 dashboard shows released relation partners', text.includes('Baltic Freight GmbH') && text.includes('Karla Brenner'));
  check('S11 dashboard never shows cloud-only entities', !text.includes('Jonas Weber') && !text.includes('Limassol'));
  check('S11 dashboard without JS errors', errors.length === 0, errors.join(' | ').slice(0, 300));

  const entityDir = join(root, 'entity');
  const pages = readdirSync(entityDir).filter(d => existsSync(join(entityDir, d, 'index.html')));
  const nordhafen = pages.find(d => d.startsWith('nordhafen-logistik-ag-'));
  check('S11 entity page for Nordhafen Logistik AG exists', Boolean(nordhafen), nordhafen || pages.join(','));
  if (nordhafen) {
    const errs = [];
    const p2 = await browser.newPage({viewport: {width: 1280, height: 900}});
    p2.on('pageerror', e => errs.push(e.message));
    const resp = await p2.goto(`${base}/entity/${nordhafen}/`);
    await p2.waitForTimeout(800);
    const body = await p2.evaluate(() => document.body.innerText);
    check('S11 entity page renders (HTTP 200, title, no JS errors)', resp.status() === 200
      && (await p2.title()).includes('Nordhafen') && errs.length === 0, `${resp.status()} ${await p2.title()}`);
    check('S11 entity page lists the released acquisition', body.includes('Baltic Freight GmbH'));
    check('S11 entity page shows no unreleased cloud facts', !body.includes('Hamburg') || !/OPERATES_IN|betreibt/i.test(body));
    await p2.screenshot({path: '/tmp/puppetnet-crossstack-entity.png', fullPage: true});
  }
  await page.screenshot({path: '/tmp/puppetnet-crossstack-dashboard.png', fullPage: false});
} finally {
  await browser.close();
  server.close();
}
