"""Read-only installation diagnostics. Does not expose account keys or contact data."""
import frappe


def run():
    names = ["WA Campaign", "WA Campaign Run", "WA Campaign Recipient", "WA Campaign Filter", "WA Template Variable", "WA Automation Settings"]
    return {"installed": "wa_automation" in frappe.get_installed_apps(),
        "doctypes": {name: bool(frappe.db.exists("DocType", name)) for name in names},
        "workspace": bool(frappe.db.exists("Workspace", "WA Automation")),
        "scheduler_event": bool(frappe.db.exists("Scheduled Job Type", {"method": "wa_automation.jobs.tick"})),
        "scheduler_disabled": bool(frappe.conf.get("disable_scheduler")),
        "settings": {key: frappe.db.get_single_value("WA Automation Settings", key) for key in ("sending_enabled", "max_recipients", "messages_per_minute", "scheduler_last_seen")},
        "campaign_count": frappe.db.count("WA Campaign"),
    }
