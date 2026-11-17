/* Campaign timestamps are stored in UTC. Display them in India time explicitly,
 * independently of the browser, user and site's configured timezone. */
(() => {
  const indianTime = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true,
  });
  function formatIST(value) {
    if (!value) return "";
    let iso = String(value).trim().replace(" ", "T");
    if (!/(Z|[+-]\d{2}:?\d{2})$/i.test(iso)) iso += "Z";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const parts = Object.fromEntries(indianTime.formatToParts(date).map(p => [p.type, p.value]));
    return `${parts.day}-${parts.month}-${parts.year} ${parts.hour}:${parts.minute}:${parts.second} ${parts.dayPeriod.toUpperCase()} IST`;
  }
  globalThis.wa_automation = globalThis.wa_automation || {};
  globalThis.wa_automation.format_ist = formatIST;
  const fields = {
    "WA Campaign": ["next_run_at"],
    "WA Campaign Run": ["scheduled_for", "started_at", "finished_at"],
    "WA Campaign Recipient": ["next_attempt_at", "dispatch_started_at", "accepted_at"],
    "WA Automation Settings": ["scheduler_last_seen"],
  };
  for (const [doctype, names] of Object.entries(fields)) {
    function formatFields(frm) {
      for (const name of names) {
        const control = frm.fields_dict[name];
        if (!control) continue;
        Object.assign(control.df, {
          formatter: formatIST, read_only: 1, hide_timezone: 1,
          description: "Indian Standard Time (Asia/Kolkata)",
        });
        control.refresh();
      }
    }
    frappe.ui.form.on(doctype, {setup: formatFields, refresh: formatFields});
  }
})();
