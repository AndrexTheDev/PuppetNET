/** Optional Chromium acceptance for the exported site, ad breakpoints and legacy drawers.
 * Serve apps/web/out, then SITE_URL=http://127.0.0.1:8080 node tests/site_browser.mjs
 * Needs Playwright and Chromium; captures only git-ignored temporary screenshots.
 */
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdirSync,writeFileSync} from 'node:fs';
const browser=await chromium.launch({headless:true,
  ...(process.env.CHROMIUM_EXECUTABLE ? {executablePath:process.env.CHROMIUM_EXECUTABLE} : {}),
  args:process.env.CHROMIUM_ARGS ? JSON.parse(process.env.CHROMIUM_ARGS) : [],
});
const base=process.env.SITE_URL || 'http://127.0.0.1:8080';
const results=[];mkdirSync('.screenshots/redesign',{recursive:true});
try{
 for(const width of [360,820,1280,1920]){
  const page=await browser.newPage({viewport:{width,height:900}});const errors=[];await page.addInitScript(()=>sessionStorage.setItem('puppetnet-intro-seen','1'));page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/');await page.waitForTimeout(500);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  const mobile=page.locator('.mobile-ad-container');assert.equal(await mobile.isVisible(),width<1100);
  assert.equal(await page.locator('.ad-header').isVisible(),width>=1100);assert.equal(await page.locator('.ad-left').isVisible(),width>=1600);
  await page.screenshot({path:`.screenshots/redesign/home-${width}.png`,fullPage:true});
  if(width<1100){await page.getByRole('button',{name:'Werbeplatz schließen'}).click();assert.equal(await mobile.count(),0)}
  await page.goto(base+'/dashboard/');await page.waitForTimeout(2000);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await page.screenshot({path:`.screenshots/redesign/dashboard-${width}.png`,fullPage:true});
  if(width<=820){await page.goto(base+'/legacy/');await page.waitForFunction(()=>window.PuppetNET?.cy?.nodes().length>0);await page.locator('#btn-inspector').click();await page.waitForTimeout(400);
   const rects=await page.evaluate(()=>({header:document.querySelector('.app-header').getBoundingClientRect().bottom,drawer:document.querySelector('.inspector').getBoundingClientRect().top,overflow:document.documentElement.scrollWidth>innerWidth}));assert.ok(rects.drawer>=rects.header-1,JSON.stringify(rects));assert.equal(rects.overflow,false);await page.screenshot({path:`.screenshots/redesign/legacy-drawer-${width}.png`,timeout:120000});
   await page.locator('#btn-inspector').click();await page.locator('#btn-rail').click();await page.waitForTimeout(400);
   assert.ok(await page.locator('.rail').evaluate(el=>el.getBoundingClientRect().top >= document.querySelector('.app-header').getBoundingClientRect().bottom-1));
   await page.screenshot({path:`.screenshots/redesign/legacy-rail-${width}.png`,timeout:120000});}
  assert.deepEqual(errors,[]);results.push({width,passed:true});await page.close();
 }
}finally{await browser.close();writeFileSync('.screenshots/redesign/results.json',JSON.stringify(results,null,2));}
console.log(results);
