"""Local-only browser fixture; never whitelisted. Temporary session is revoked by cleanup."""
import json
import os
from pathlib import Path
import uuid

import frappe

STATE = Path("/tmp/wa-automation-browser-fixture.json")


def setup():
    from frappe.sessions import Session
    frappe.local.request = frappe._dict(cookies={})
    frappe.local.request_ip = "127.0.0.1"
    session = Session("Administrator", full_name="Administrator", user_type="System User")
    name = "WA-UI-TEST-" + uuid.uuid4().hex[:8]
    account = frappe.get_doc({"doctype":"Chat Channel Account", "name":name, "account_name":name,
        "channel_type":"Interakt", "is_active":1, "interakt_default_country_code":"+91"})
    account.db_insert()
    row = frappe.get_doc({"doctype":"WA Interakt Template Catalog", "parent":name, "parenttype":"Chat Channel Account",
        "parentfield":"interakt_template_catalog", "idx":1, "template_name":"wa_ui_test", "language_code":"en",
        "body_preview":"Hello {{1}}, this is a test template.", "body_variable_count":1})
    row.db_insert()
    STATE.write_text(json.dumps({"sid": session.sid, "account": name}))
    os.chmod(STATE, 0o600)
    frappe.db.commit()
    return {"fixture_ready": True}


def cleanup():
    from frappe.sessions import delete_session
    if not STATE.exists():
        return
    data = json.loads(STATE.read_text())
    campaigns = frappe.get_all("WA Campaign", filters={"channel_account":data["account"]}, pluck="name")
    for name in campaigns:
        if frappe.db.exists("WA Campaign Run", {"campaign":name}):
            frappe.throw("Unexpected run on browser fixture; refusing to remove audit records.")
        for dt in ("WA Campaign Filter", "WA Template Variable"):
            frappe.db.delete(dt, {"parent":name})
        frappe.db.delete("WA Campaign", {"name":name})
    frappe.db.delete("WA Interakt Template Catalog", {"parent":data["account"]})
    frappe.db.delete("Chat Channel Account", {"name":data["account"]})
    delete_session(data["sid"], user="Administrator", reason="WA Automation browser test completed")
    frappe.db.commit()
    STATE.unlink()
    return {"fixture_removed": True, "session_revoked": True}
