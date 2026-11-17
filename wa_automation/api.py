import json
from datetime import timedelta

import frappe
from frappe.utils import cint

from wa_automation import audience
from wa_automation.core import schedule_next, utcnow, as_utc, normalize_number, stable_key
from wa_automation.permissions import require_app, require_sender


def campaign(name, write=False):
    require_app()
    doc = frappe.get_doc("WA Campaign", name)
    doc.check_permission("write" if write else "read")
    return doc


@frappe.whitelist()
def fields(reference_doctype):
    require_app()
    return list(audience.available_fields(reference_doctype).values())


@frappe.whitelist()
def templates(channel_account, refresh=0, cached_only=0):
    require_app()
    config = {"channel_account": channel_account}
    # The picker must never wait for Interakt while opening a form. This longer
    # lived display cache is not used to approve templates for sending.
    display_key = f"wa_automation:template_picker:{channel_account}"
    if cint(cached_only):
        from wa_chat_hub.interakt.templates_api import _load_manual_catalog, _merge_template_lists
        account = audience.account_for(config)
        cached = frappe.cache().get_value(f"wa_interakt_templates::{channel_account}")
        if cached is not None:
            frappe.cache().set_value(display_key, cached, expires_in_sec=86400)
        else:
            cached = frappe.cache().get_value(display_key)
        catalog = _merge_template_lists(cached or [], _load_manual_catalog(account))
    else:
        catalog = audience.approved_templates(config, bool(cint(refresh)))
        frappe.cache().set_value(display_key, catalog, expires_in_sec=86400)
    keys = {"name", "language_code", "body_preview", "header_preview", "header_format", "body_variable_count", "body_variables", "header_variable_count", "button_variable_count", "buttons"}
    return [{**{k: v for k, v in t.items() if k in keys}, "language_code": language}
        for t in catalog for language in (t.get("languages") or [t.get("language_code") or "en"])]


@frappe.whitelist(methods=["POST"])
def preview(name, start=0, page_length=50):
    doc = campaign(name)
    rows = audience.build_audience(doc.as_dict())
    start = max(0, cint(start))
    size = min(100, max(1, cint(page_length)))
    public = [{k: r.get(k) for k in ("reference_name", "masked_phone", "status", "last_error", "preview")} for r in rows[start:start + size]]
    return {"matched": len(rows), "eligible": sum(r["status"] == "Pending" for r in rows), "skipped": sum(r["status"] == "Skipped" for r in rows), "rows": public, "start": start, "page_length": size}


@frappe.whitelist()
def schedule_preview(name):
    doc = campaign(name)
    anchor = utcnow()
    result = []
    from zoneinfo import ZoneInfo
    for _ in range(3):
        instant = schedule_next(doc.as_dict(), anchor)
        if not instant:
            break
        result.append(instant.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%a, %d %b %Y %I:%M:%S %p IST"))
        anchor = instant
    return result


@frappe.whitelist(methods=["POST"])
def send_now(name, request_key):
    doc = campaign(name, write=True)
    require_sender()
    if not request_key or len(request_key) > 100:
        frappe.throw("A valid send request ID is required.")
    if doc.delivery_mode != "Now":
        frappe.throw("Use Activate Schedule for scheduled campaigns, or choose Now for immediate bulk sending.")
    audience.validate_config(doc.as_dict(), ready=True)
    from wa_automation.jobs import create_run
    return create_run(doc, utcnow(), "manual:" + request_key)


@frappe.whitelist(methods=["POST"])
def send_test(name, reference_name, test_number, request_key):
    doc = campaign(name, write=True)
    require_sender()
    if not cint(frappe.db.get_single_value("WA Automation Settings", "sending_enabled", cache=False)):
        frappe.throw("WA Automation sending is disabled in Settings.")
    if not request_key or len(request_key) > 100:
        frappe.throw("A valid test request ID is required.")
    account = audience.account_for(doc.as_dict())
    number, country, national = normalize_number(test_number, account.interakt_default_country_code or "+91")
    rows = audience.build_audience(doc.as_dict())
    selected = next((row for row in rows if row["reference_name"] == reference_name and row["status"] == "Pending"), None)
    if not selected:
        frappe.throw("Choose an eligible source record from Preview Audience to personalize the test.")
    with frappe.cache().lock(f"wa_automation:campaign:{name}", timeout=30, blocking_timeout=5):
        key = stable_key(name, "test:" + request_key)
        existing = frappe.db.get_value("WA Campaign Run", {"run_key": key}, "name")
        if existing:
            return existing
        snapshot = doc.as_dict()
        snapshot["_test_phone"] = number
        run = frappe.get_doc({"doctype":"WA Campaign Run", "campaign":name, "owner":doc.owner,
            "status":"Sending", "is_test":1, "run_key":key, "scheduled_for":utcnow().replace(tzinfo=None),
            "snapshot_json":json.dumps(snapshot, default=str), "matched":1, "eligible":1}).insert(ignore_permissions=True)
        payload = json.loads(selected["payload_json"])
        payload.update(countryCode=country, phoneNumber=national)
        selected.update(doctype="WA Campaign Recipient", campaign=name, run=run.name, owner=doc.owner,
            reference_doctype=doc.reference_doctype, recipient_key=stable_key(run.name, reference_name),
            phone=number, masked_phone="••••" + number[-4:], payload_json=json.dumps(payload), is_test=1)
        frappe.get_doc(selected).insert(ignore_permissions=True)
        frappe.db.commit()
    from wa_automation.jobs import enqueue_run
    enqueue_run(run.name)
    return run.name


@frappe.whitelist(methods=["POST"])
def set_state(name, action):
    doc = campaign(name, write=True)
    require_sender()
    with frappe.cache().lock(f"wa_automation:campaign:{name}", timeout=30, blocking_timeout=5):
        doc.reload()
        if action == "pause":
            frappe.db.set_value("WA Campaign", name, {"status": "Paused", "next_run_at": None})
        elif action == "end":
            frappe.db.set_value("WA Campaign", name, {"status": "Completed", "next_run_at": None})
            frappe.db.set_value("WA Campaign Recipient", {"campaign": name, "status": ["in", ["Pending", "Retry"]]},
                {"status": "Skipped", "last_error": "Campaign ended before dispatch."})
            from wa_automation.jobs import update_totals, OPEN_RUNS
            for run in frappe.get_all("WA Campaign Run", filters={"campaign": name, "is_test": 0, "status": ["in", OPEN_RUNS]}, pluck="name"):
                update_totals(run)
        elif action in {"activate", "resume"}:
            audience.validate_config(doc.as_dict(), ready=True)
            audience.selected_template(doc.as_dict(), audience.approved_templates(doc.as_dict(), refresh=True))
            if doc.delivery_mode == "Now":
                next_run = None
            else:
                next_run = schedule_next(doc.as_dict())
                unfinished = frappe.db.exists("WA Campaign Run", {"campaign": name, "is_test": 0, "status": ["in", ["Queued", "Preparing", "Sending"]]})
                if not next_run and not (action == "resume" and unfinished):
                    frappe.throw("There are no future occurrences in this schedule.")
            frappe.db.set_value("WA Campaign", name, {"status": "Active", "next_run_at": next_run.replace(tzinfo=None) if next_run else None, "last_error": ""})
        else:
            frappe.throw("Unknown campaign action.")
        frappe.db.commit()
    return {"status": frappe.db.get_value("WA Campaign", name, "status")}


@frappe.whitelist(methods=["POST"])
def exclude(name, reference_name):
    doc = campaign(name, write=True)
    if doc.status == "Active":
        frappe.throw("Pause the campaign before changing exclusions.")
    values = {v.strip() for v in (doc.excluded_records or "").splitlines() if v.strip()}
    values.add(reference_name)
    doc.excluded_records = "\n".join(sorted(values))
    doc.save()
    return True
