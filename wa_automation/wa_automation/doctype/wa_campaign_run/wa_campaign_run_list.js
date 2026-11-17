frappe.listview_settings["WA Campaign Run"] = {
  formatters: Object.fromEntries(["scheduled_for", "started_at", "finished_at"]
    .map(name => [name, value => wa_automation.format_ist(value)])),
};
