import frappe


def before_install():
    frappe.cache().delete_value("app_modules")
    frappe.cache().delete_value("installed_app_modules")
    frappe.setup_module_map(include_all_apps=True)
    if "wa_automation" not in frappe.local.app_modules.get("wa_automation", []):
        frappe.throw("WA Automation module metadata was not discovered.")


def after_install():
    for name in ("WA Automation User", "WA Automation Sender", "WA Automation Manager"):
        if not frappe.db.exists("Role", name):
            frappe.get_doc({"doctype": "Role", "role_name": name}).insert(ignore_permissions=True)
    after_migrate()


def after_migrate():
    if frappe.db.exists("DocType", "WA Campaign Recipient"):
        frappe.db.add_index("WA Campaign Recipient", ["run", "status"], "wa_run_status")
        frappe.db.add_index("WA Campaign Recipient", ["campaign", "phone", "status"], "wa_campaign_phone")
        frappe.db.add_index("WA Campaign Run", ["campaign", "status"], "wa_campaign_status")
        _ensure_settings_defaults()


def _ensure_settings_defaults():
    doctype = "WA Automation Settings"
    # Singles has no document name column. Read its stored fields directly and
    # lock them while initializing, since the scheduler also writes a heartbeat.
    stored = frappe.db.get_singles_dict(doctype, for_update=True)
    defaults = dict(sending_enabled=1, max_recipients=10000, messages_per_minute=60,
                    max_attempts=3, schedule_grace_minutes=15)
    missing = {field: value for field, value in defaults.items() if field not in stored}
    if missing:
        # Update only missing fields, preserving saved settings and the heartbeat.
        frappe.db.set_single_value(doctype, missing)


def sync_app():
    """Targeted app schema refresh; does not migrate or modify unrelated apps."""
    from frappe.model.sync import sync_for
    from frappe.core.doctype.scheduled_job_type.scheduled_job_type import insert_single_event
    before_install()
    sync_for("wa_automation", force=True)
    after_install()
    insert_single_event("Cron", "wa_automation.jobs.tick", "* * * * *")
    frappe.clear_cache()
    frappe.db.commit()
