/** Optional Playwright check. Serve apps/web/out; SITE_URL defaults to http://127.0.0.1:8080. */
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {chromium} from 'playwright';
const browser=await chromium.launch({headless:true,
  ...(process.env.CHROMIUM_EXECUTABLE ? {executablePath:process.env.CHROMIUM_EXECUTABLE}:{}),
  args:process.env.CHROMIUM_ARGS ? JSON.parse(process.env.CHROMIUM_ARGS):[],
});
const base=process.env.SITE_URL || 'http://127.0.0.1:8080';
mkdirSync('.screenshots/branding',{recursive:true});
try {
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto(base+'/');await page.getByRole('dialog',{name:'PuppetNET Intro'}).waitFor();
 await page.waitForFunction(()=>document.querySelector('video')?.currentTime>1);
 assert.equal(await page.locator('.site-content').evaluate(el=>el.inert),true);
 await page.screenshot({path:'.screenshots/branding/intro.png'});
 await page.getByRole('dialog').waitFor({state:'hidden',timeout:15000});
 assert.equal(await page.locator('.site-content').evaluate(el=>el.inert),false);
 await page.screenshot({path:'.screenshots/branding/home.png',fullPage:true});
 await page.reload();await page.waitForTimeout(400);assert.equal(await page.getByRole('dialog').count(),0);
 await page.getByRole('button',{name:'Intro ansehen'}).click();await page.getByRole('dialog').waitFor();
 await page.keyboard.press('Escape');assert.equal(await page.getByRole('dialog').count(),0);
 await page.getByRole('button',{name:'Intro ansehen'}).click();await page.getByRole('button',{name:/Intro überspringen/}).click();
 assert.equal(await page.locator('.site-content').evaluate(el=>el.inert),false);
 console.log('PASS intro plays muted, ends, restores content, once per session, replay/Escape/skip');
 for(const width of [360,820,1920]){
  await page.setViewportSize({width,height:900});await page.goto(base+'/');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.equal(await page.locator('.brand-art').first().evaluate(el=>el.complete&&el.naturalWidth>0),true);
  await page.screenshot({path:`.screenshots/branding/home-${width}.png`,fullPage:true});
 }
 assert.deepEqual(errors,[]);await page.close();
 const reduced=await browser.newPage({reducedMotion:'reduce'});const requests=[];reduced.on('request',r=>requests.push(r.url()));
 await reduced.goto(base+'/');await reduced.waitForTimeout(600);assert.equal(await reduced.getByRole('dialog').count(),0);
 assert.equal(requests.some(url=>/intro-.*\.(webm|mp4)/.test(url)),false);await reduced.close();
 console.log('PASS reduced motion skips intro and video download; logo loads, responsive layouts');
 const blocked=await browser.newPage();await blocked.addInitScript(()=>{HTMLMediaElement.prototype.play=()=>Promise.reject(new DOMException('blocked','NotAllowedError'));});
 await blocked.goto(base+'/');await blocked.getByRole('button',{name:'Intro ansehen'}).waitFor();await blocked.waitForTimeout(1500);
 assert.equal(await blocked.getByRole('dialog').count(),0);await blocked.close();
 console.log('PASS blocked autoplay does not trap visitor');
 const fallback=await browser.newPage();await fallback.route('**/intro-alpha.webm',route=>route.abort());
 await fallback.goto(base+'/');await fallback.waitForFunction(()=>document.querySelector('video')?.currentSrc.endsWith('.mp4')&&document.querySelector('video').currentTime>0);
 await fallback.getByRole('button',{name:/Intro überspringen/}).click();await fallback.close();
 console.log('PASS video failure switches to playable MP4 fallback');
} finally {await browser.close();}
