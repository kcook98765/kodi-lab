/* Real route HTML and controller, with every browser request intercepted. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const modules = process.env.KODI_LAB_BROWSER_TOOLS;
const evidence = process.env.KODI_LAB_PROJECT_UI_EVIDENCE;
assert(modules && evidence, 'Set browser tools and exported local-project fixtures');
const {chromium} = require(path.join(modules, 'playwright'));
const AxeBuilder = require(path.join(modules, '@axe-core/playwright')).default;
const fixture = JSON.parse(fs.readFileSync(path.join(evidence, 'fixtures.json')));
const origin = 'https://project-fixture.invalid';
const assets = path.resolve(__dirname, '../../src/kodi_lab/web/static');
const report = {method:'Real Web route fixtures, fake broker; all Chromium requests intercepted', layouts:[], errors:[], mutations:[], polling:[]};
let activeBrowser;
(async () => {
  const browser = activeBrowser = await chromium.launch({headless:true});
  async function open(name, width, failStatus = false) {
    const context = await browser.newContext({viewport:{width, height:1000}, colorScheme:width === 390 ? 'dark' : 'light'});
    let ready = false, reads = 0;
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, origin, 'No external requests');
      if (request.method() !== 'GET') {
        report.mutations.push(url.pathname);
        assert.equal(url.pathname, '/development-session/projects');
        const body = new URLSearchParams(request.postData());
        assert.equal(body.get('project'), 'hub');
        assert(body.get('client_id') && body.get('csrf_token'));
        ready = true;
        return route.fulfill({contentType:'text/html', body:fixture.pages.ready.replaceAll('https://127.0.0.1', origin)});
      }
      if (url.pathname === '/development-session') {
        return route.fulfill({contentType:'text/html', body:fixture.pages[ready ? 'ready' : name].replaceAll('https://127.0.0.1', origin)});
      }
      if (url.pathname.startsWith('/static/')) return route.fulfill({path:path.join(assets, path.basename(url.pathname))});
      if (url.pathname === '/api/development-session') return route.fulfill({json:{latest_generation:0,current:null}});
      if (url.pathname.startsWith('/api/development-projects/')) {
        reads++;
        if (failStatus) return route.fulfill({status:409, json:{code:'project_not_authorized',message:'Access changed'}});
        ready = true;
        return route.fulfill({json:fixture.ready});
      }
      return route.fulfill({status:404,body:'Outside project fixture'});
    });
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    await page.goto(`${origin}/development-session`);
    return {page, context, reads:() => reads};
  }
  for (const width of [390, 1440]) {
    for (const name of ['select','ready','error','unconfigured']) {
      const {page,context} = await open(name,width);
      await page.getByRole('heading',{name:'Select an add-on',exact:true}).waitFor();
      if (name === 'select') {
        await page.getByLabel('Add-on project', {exact:true}).selectOption('hub');
        await page.getByRole('button',{name:'Choose add-on project'}).focus();
        await page.keyboard.press('Enter');
        await page.getByRole('link',{name:'Choose Kodi version',exact:true}).waitFor();
      }
      if (name === 'ready') {
        const link = page.getByRole('link',{name:'Choose Kodi version',exact:true});
        assert((await link.getAttribute('href')).includes(fixture.ready.workspace_id));
        assert(await page.getByText('Project setup does not establish native readiness', {exact:false}).isVisible());
        await page.screenshot({path:path.join(evidence,`ready-${width}.png`),fullPage:true});
      }
      if (name === 'error') assert((await page.getByRole('alert').textContent()).includes('relative to the approved development root'));
      if (name === 'unconfigured') {
        assert(await page.getByRole('link',{name:'Set up a local development root'}).isVisible());
        assert.equal(await page.getByRole('button',{name:'Choose add-on project'}).count(),0);
      }
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow,false,`${name} overflows at ${width}`);
      const axe = await new AxeBuilder({page}).include('section[aria-labelledby="local-project-heading"]').analyze();
      assert.deepEqual(axe.violations.map(v=>v.id),[],`${name} accessibility`);
      report.layouts.push({name,width,overflow,accessibilityViolations:axe.violations.length});
      await context.close();
    }
  }
  const pending = await open('preparing',390);
  await pending.page.getByRole('link',{name:'Choose Kodi version',exact:true}).waitFor();
  assert.equal(pending.reads(),1);
  report.polling.push('Completion refreshes the saved project through GET only');
  await pending.context.close();
  const failed = await open('preparing',390,true);
  await failed.page.getByText('Project status could not be confirmed.',{exact:false}).waitFor();
  await failed.page.waitForTimeout(2300);
  assert.equal(failed.reads(),1,'Uncertain status must not resubmit onboarding');
  report.polling.push('Failed status stops polling and offers saved-operation refresh');
  await failed.context.close();
  assert.deepEqual(report.errors,[]);
  assert.equal(report.mutations.length,2,'Only explicit keyboard submissions mutate');
  fs.writeFileSync(path.join(evidence,'browser-report.json'),JSON.stringify(report,null,2));
  await browser.close();
  console.log(`PASS: ${report.layouts.length} layouts, keyboard onboarding, scoped accessibility, completion and uncertainty polling`);
})().catch(async error=>{console.error(error);await activeBrowser?.close();process.exitCode=1;});
