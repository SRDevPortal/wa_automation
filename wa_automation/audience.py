import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe.model import get_permitted_fields
from frappe.utils import cint, get_system_timezone

from wa_automation.core import compile_filters, normalize_number, render_values, preview_text, utcnow, as_utc, SUCCESS

SOURCES = {"Patient", "Lead", "CRM Lead"}
TYPES = {"Data", "Select", "Link", "Dynamic Link", "Date", "Datetime", "Time", "Int", "Float", "Currency", "Percent", "Check", "Small Text", "Text", "Long Text", "Read Only"}


def available_fields(doctype):
    if doctype not in SOURCES or not frappe.db.exists("DocType", doctype):
        frappe.throw("Select an installed Patient, Lead or CRM Lead DocType.")
    if not frappe.has_permission(doctype, "read"):
        frappe.throw("You cannot read this audience.", frappe.PermissionError)
    meta = frappe.get_meta(doctype)
    permitted = set(get_permitted_fields(doctype, user=frappe.session.user, permission_type="read", ignore_virtual=True))
    fields = {f.fieldname: {"fieldname": f.fieldname, "label": f.label or f.fieldname, "fieldtype": f.fieldtype, "options": f.options} for f in meta.fields if f.fieldname in permitted and f.fieldtype in TYPES and not f.get("is_virtual")}
    for name, kind in (("name", "Data"), ("creation", "Datetime"), ("modified", "Datetime"), ("owner", "Link")):
        if name in permitted:
            fields[name] = {"fieldname": name, "label": name.title(), "fieldtype": kind}
    return fields


def account_for(config):
    doc = frappe.get_doc("Chat Channel Account", config.get("channel_account"))
    doc.check_permission("read")
    if doc.channel_type != "Interakt" or not doc.is_active:
        frappe.throw("Select an active Interakt account.")
    return doc


def validate_config(config, ready=False):
    fields = available_fields(config.get("reference_doctype"))
    account_for(config)
    for key in ("phone_field", "sort_field"):
        value = config.get(key)
        if value and value not in fields:
            frappe.throw(f"{key}: field is unavailable or not permitted.")
    if config.get("sort_direction") not in {"asc", "desc"}:
        frappe.throw("Invalid sort direction.")
    if config.get("match_mode") not in {"All", "Any"}:
        frappe.throw("Invalid filter match mode.")
    try:
        ZoneInfo(config.get("timezone") or "Asia/Kolkata")
        compile_filters(config.get("filters") or [], fields, utcnow(), config.get("timezone") or "Asia/Kolkata", get_system_timezone())
        if int(config.get("minimum_gap_days") or 0) < 0:
            raise ValueError("Minimum gap cannot be negative.")
        for mapping in config.get("variables") or []:
            if mapping.get("source_type") == "Field" and mapping.get("field_name") not in fields:
                raise ValueError("A mapped variable uses a field that is unavailable or not permitted.")
        # Validate variable structure without requiring any actual patient values.
        render_values(config.get("variables") or [], dict.fromkeys(fields, "Example"), fields)
    except (ValueError, KeyError) as exc:
        frappe.throw(str(exc))
    if ready and not config.get("consent_confirmed"):
        frappe.throw("Confirm this audience has permission to receive WhatsApp messages.")
    return fields


def approved_templates(config, refresh=False):
    from wa_chat_hub.interakt.templates_api import fetch_approved_templates, _load_manual_catalog
    account = account_for(config)
    if not account.get_password("interakt_api_key", raise_exception=False):
        manual = _load_manual_catalog(account)
        if manual:
            return manual
        frappe.throw("Configure the Interakt API key or an approved template catalog on this account.")
    return fetch_approved_templates(config["channel_account"], force_refresh=refresh)


def selected_template(config, catalog):
    from wa_chat_hub.interakt.templates_api import find_approved_template
    template = find_approved_template(catalog, config.get("template_name"), config.get("language_code") or "en")
    if not template:
        frappe.throw("The selected template and language are not available on this Interakt account. Refresh Templates.")
    return template


def source_records(config, anchor):
    fields = validate_config(config, ready=True)
    filters = compile_filters(config.get("filters") or [], fields, anchor, config.get("timezone") or "Asia/Kolkata", get_system_timezone())
    needed = {"name", config["phone_field"]}
    needed.update(row["field_name"] for row in config.get("variables", []) if row.get("source_type") == "Field")
    # Respect customary source-level suppression.
    for field in ("disabled", "unsubscribed", "status"):
        if field in fields:
            needed.add(field)
    maximum = max(1, min(cint(frappe.db.get_single_value("WA Automation Settings", "max_recipients") or 10000), 100000))
    # Permission-aware Frappe query; do not replace with get_all.
    rows = frappe.get_list(config["reference_doctype"], fields=sorted(needed),
        filters=filters if config.get("match_mode") != "Any" else [],
        or_filters=filters if config.get("match_mode") == "Any" else [],
        order_by=f"{config.get('sort_field') or 'creation'} {config.get('sort_direction') or 'desc'}, name asc",
        limit_page_length=maximum + 1)
    if len(rows) > maximum:
        frappe.throw(f"Audience exceeds the {maximum:,} record limit. Narrow the filters or ask a manager to raise the limit.")
    return fields, rows


def suppression_reason(config, record):
    if record.get("name") in {v.strip() for v in (config.get("excluded_records") or "").splitlines() if v.strip()}:
        return "Manually excluded"
    if record.get("disabled") or record.get("status") in {"Disabled", "Do Not Contact"}:
        return "Disabled or do not contact"
    if record.get("unsubscribed"):
        return "Opted out"
    return None


def recent_history(config):
    if not config.get("name") or (config.get("repeat_policy") != "Only new recipients" and not config.get("minimum_gap_days")):
        return {}
    rows = frappe.get_all("WA Campaign Recipient", filters={"campaign": config["name"], "is_test": 0},
        or_filters=[["accepted_at", "is", "set"], ["status", "in", ["Unknown", "Sending"]]],
        fields=["phone", "accepted_at", "dispatch_started_at"], limit_page_length=0)
    history = {}
    for row in rows:
        instant = row.accepted_at or row.dispatch_started_at
        if instant:
            previous = history.get(row.phone)
            history[row.phone] = max(previous, as_utc(instant)) if previous else as_utc(instant)
    return history


def build_audience(config, anchor=None, catalog=None):
    anchor = as_utc(anchor or utcnow())
    fields, records = source_records(config, anchor)
    account = account_for(config)
    catalog = catalog if catalog is not None else approved_templates(config, refresh=True)
    selected = selected_template(config, catalog)
    from wa_chat_hub.interakt.templates_api import resolve_approved_template
    history = recent_history(config)
    results = []
    seen = {}
    for record in records:
        row = {"reference_name": record.name, "status": "Pending", "phone": "", "masked_phone": "", "last_error": ""}
        try:
            reason = suppression_reason(config, record)
            if reason:
                raise ValueError(reason)
            phone, country, national = normalize_number(record.get(config["phone_field"]), account.interakt_default_country_code or "+91")
            row.update(phone=phone, masked_phone="••••" + phone[-4:])
            if phone in history:
                if config.get("repeat_policy") == "Only new recipients":
                    raise ValueError("Previously submitted in this campaign")
                if anchor - history[phone] < timedelta(days=int(config.get("minimum_gap_days") or 0)):
                    raise ValueError("Minimum message gap has not elapsed")
            values = render_values(config.get("variables") or [], record, fields)
            template = {"template_name": config["template_name"], "language_code": config.get("language_code") or "en", **values}
            if config.get("header_media_url"):
                template["header_media_url"] = config["header_media_url"]
            template = resolve_approved_template(config["channel_account"], template, approved_templates=catalog)
            payload = {"countryCode": country, "phoneNumber": national, "type": "Template", "template": {"name": template["template_name"], "languageCode": template["language_code"], "bodyValues": template.get("body_values") or [], "headerValues": template.get("header_values") or [], "buttonValues": template.get("button_values") or {}}}
            row["payload_json"] = json.dumps(payload)
            row["preview"] = preview_text(selected.get("body_preview") or config.get("template_preview"), template.get("body_values") or [])
            if phone in seen:
                earlier = seen[phone]
                if earlier.get("payload_json") != row["payload_json"]:
                    earlier.update(status="Skipped", last_error="Shared number has conflicting personalization")
                    raise ValueError("Shared number has conflicting personalization")
                raise ValueError("Duplicate number in this run")
            seen[phone] = row
        except (ValueError, frappe.ValidationError) as exc:
            row.update(status="Skipped", last_error=str(exc)[:500])
        results.append(row)
    return results


def recheck_recipient(config, recipient):
    """Access and preferences are live; the payload and filter audience stay frozen."""
    fields = validate_config(config, ready=True)
    doc = frappe.get_doc(config["reference_doctype"], recipient.reference_name)
    doc.check_permission("read")
    # Read suppression fields server-side even if they are not exposed in the builder.
    reason = suppression_reason(config, doc.as_dict())
    if reason:
        raise ValueError(reason)
    account = account_for(config)
    number = normalize_number(doc.get(config["phone_field"]), account.interakt_default_country_code or "+91")[0]
    if number != recipient.phone and not config.get("_test_phone"):
        raise ValueError("Phone changed after this run was prepared")
    if config.get("phone_field") not in fields:
        raise ValueError("Phone field is no longer permitted")
    return account
