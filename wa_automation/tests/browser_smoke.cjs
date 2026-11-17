const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.WA_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const statePath = process.env.WA_FIXTURE_PATH || '/tmp/wa-automation-browser-fixture.json';
  const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
  const output = process.env.WA_ARTIFACT_DIR || path.resolve(__dirname, '../../../../../test-results/wa-automation');
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({channel: process.env.WA_BROWSER_CHANNEL || 'chrome', headless: true});
  let page, stage = 'workspace';
  const failures = [];
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    await context.addCookies([{name:'sid', value:state.sid, domain:'localhost', path:'/'}]);
    await context.route('**/*', route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.hostname !== 'localhost' || /wa_automation\.api\.(send_now|set_state|send_test)/.test(url.pathname)) return route.abort();
      return route.continue();
    });
    page = await context.newPage();
    page.on('response', async response => {
      if (response.status() >= 400 && response.url().includes('/api/')) failures.push({url:response.url().split('?')[0],status:response.status(),body:(await response.text()).slice(0,1600)});
    });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://localhost:8000/app/wa-automation', {waitUntil:'domcontentloaded'});
    await page.waitForFunction(() => window.frappe?.boot, {timeout:30000});
    await page.getByText('Campaigns', {exact:true}).first().waitFor();
    await page.screenshot({path:path.join(output,'workspace.png'), fullPage:true});
    stage = 'new campaign';
    await page.evaluate(() => frappe.new_doc('WA Campaign'));
    await page.waitForFunction(() => window.cur_frm?.doctype === 'WA Campaign' && cur_frm.wa_fields?.length);
    stage = 'account selection';
    await page.evaluate(async account => {
      await cur_frm.set_value({title:'WA Automation browser test', channel_account:account, consent_confirmed:1});
    }, state.account);
    const picker = page.locator('[data-fieldname="template_picker"] select');
    stage = 'template selection';
    await picker.locator('option').filter({hasText:'wa_ui_test'}).waitFor({state:'attached'});
    await picker.selectOption({label:'wa_ui_test (en)'});
    await page.waitForFunction(() => cur_frm.doc.variables?.length === 1);
    assert.equal(await page.evaluate(() => cur_frm.doc.phone_field), 'mobile');
    stage = 'save campaign';
    // Exercise the Fixed control through the editable grid, not only the model.
    await page.evaluate(async () => {
      const row = cur_frm.doc.variables[0];
      await frappe.model.set_value(row.doctype, row.name, 'source_type', 'Fixed');
    });
    const fixedCell = page.locator('[data-fieldname="variables"] .grid-body .rows .grid-row [data-fieldname="fixed_value"]').first();
    await fixedCell.click();
    const fixedInput = fixedCell.locator('textarea, input').first();
    await fixedInput.waitFor({state:'visible'});
    await fixedInput.fill('Audit fixed value');
    await fixedInput.press('Tab');
    await page.waitForFunction(() => cur_frm.doc.variables[0].fixed_value === 'Audit fixed value');
    await page.evaluate(async () => {
      cur_frm.add_child('filters', {field_name:'name',operator:'=',value:'NO-SUCH-WA-UI-TEST-PATIENT'});
      await cur_frm.set_value({delivery_mode:'Weekly',start_date:'2099-01-01',monday:1,send_time:'10:00:00'});
      cur_frm.refresh_field('filters');
      await cur_frm.save();
    });
    await page.waitForFunction(() => !cur_frm.is_new() && cur_frm.doc.status === 'Draft');
    assert.equal(await page.evaluate(() => Boolean(cur_frm.fields_dict.consent_field || cur_frm.fields_dict.opt_out_field)), false);
    stage = 'preview';
    await page.getByRole('button', {name:'Preview Audience',exact:true}).click();
    await page.getByText('0 matching records', {exact:false}).waitFor();
    assert.equal(await page.evaluate(() => cur_frm.doc.status), 'Draft');
    await page.screenshot({path:path.join(output,'campaign-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(() => window.cur_frm?.doctype === 'WA Campaign' && cur_frm.wa_fields?.length);
    assert.equal(await page.evaluate(() => cur_frm.doc.variables[0].fixed_value), 'Audit fixed value');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    assert.equal(overflow, false, 'Mobile page must not overflow horizontally');
    await page.screenshot({path:path.join(output,'campaign-mobile.png'),fullPage:true});
    const fields = await page.evaluate(() => ({count:cur_frm.wa_fields.length, template:cur_frm.doc.template_name, variables:cur_frm.doc.variables.length, weekdays:cur_frm.doc.monday, status:cur_frm.doc.status}));
    assert.deepEqual(errors, [], 'Browser JavaScript errors');
    fs.writeFileSync(path.join(output,'browser-results.json'), JSON.stringify({passed:true,fields,errors,live_messages_sent:0},null,2));
    console.log(JSON.stringify({passed:true,fields,errors,live_messages_sent:0}));
  } catch (error) {
    if (page) await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});
    console.error(JSON.stringify({stage,failures}));
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode=1; });
