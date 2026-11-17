const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {Intl, Date, frappe:{ui:{form:{on(){}}}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../public/js/campaign_time.js'),'utf8'),context);
const format = context.wa_automation.format_ist;
test('stored UTC schedule is shown in IST, including seconds', () => {
  assert.equal(format('2026-11-04 08:35:50'),'04-11-2026 02:05:50 PM IST');
  assert.equal(format('2026-11-04T08:35:50.123456'),'04-11-2026 02:05:50 PM IST');
});
test('midnight, day rollover, and explicit offsets', () => {
  assert.equal(format('2026-11-04 18:30:00'),'05-11-2026 12:00:00 AM IST');
  assert.equal(format('2026-11-04T14:05:50+05:30'),'04-11-2026 02:05:50 PM IST');
});
test('empty or invalid timestamps do not create bogus dates', () => {
  for (const value of [null,undefined,'','bad date']) assert.equal(format(value),'');
});
