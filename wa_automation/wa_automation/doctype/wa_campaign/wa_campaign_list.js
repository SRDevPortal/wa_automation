frappe.listview_settings["WA Campaign"] = {
  add_fields: ["status", "delivery_mode", "next_run_at"],
  formatters: {next_run_at: value => wa_automation.format_ist(value)},
  get_indicator(doc) {
    return [__(doc.status), {Draft: "gray", Active: "green", Paused: "orange", Completed: "blue"}[doc.status], "status,=," + doc.status];
  }
};
