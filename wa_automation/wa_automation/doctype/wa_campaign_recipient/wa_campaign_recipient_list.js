frappe.listview_settings["WA Campaign Recipient"] = {
  formatters: Object.fromEntries(["next_attempt_at", "dispatch_started_at", "accepted_at"]
    .map(name => [name, value => wa_automation.format_ist(value)])),
};
