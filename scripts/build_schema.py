"""Generate the native Frappe schemas; run only when changing the app schema."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "wa_automation" / "wa_automation"


def field(name, kind="Data", label=None, **kwargs):
    return dict(fieldname=name, fieldtype=kind, label=label or name.replace("_", " ").title(), **kwargs)


def schema(name, fields, child=False, single=False, readonly=False, editable=False):
    slug = name.lower().replace(" ", "_")
    path = ROOT / "doctype" / slug
    path.mkdir(parents=True, exist_ok=True)
    (path / "__init__.py").touch()
    roles = ["System Manager", "WA Automation Manager"]
    permissions = [] if child else [dict(role=r, read=1, write=0 if readonly else 1, create=0 if readonly else 1, delete=0, report=1, export=0) for r in roles]
    if not child and not single:
        permissions.append(dict(role="WA Automation User", read=1, write=0 if readonly else 1, create=0 if readonly else 1, report=1))
    if not child:
        permissions += [dict(role=r, permlevel=1, read=1, write=1) for r in roles]
    doc = dict(doctype="DocType", name=name, module="WA Automation", engine="InnoDB", custom=0, istable=int(child), issingle=int(single), track_changes=int(not child), fields=fields, field_order=[f["fieldname"] for f in fields], permissions=permissions, sort_field="modified", sort_order="DESC")
    if not child and not single:
        doc.update(autoname="hash", title_field="title" if any(f["fieldname"] == "title" for f in fields) else None)
    if editable:
        doc["editable_grid"] = 1
    (path / (slug + ".json")).write_text(json.dumps(doc, indent=2) + "\n")
    controller = path / (slug + ".py")
    if not controller.exists():
        controller.write_text("from frappe.model.document import Document\n\n\nclass " + name.replace(" ", "") + "(Document):\n    pass\n")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "__init__.py").touch()
    (ROOT / "doctype").mkdir(exist_ok=True)
    (ROOT / "doctype" / "__init__.py").touch()
    schema("WA Campaign Filter", [
        field("field_name", reqd=1, in_list_view=1),
        field("operator", "Select", options="=\n!=\n>\n<\n>=\n<=\ncontains\nstarts with\nin\nnot in\nbetween\nis\nlast days\nnext days", default="=", reqd=1, in_list_view=1),
        field("value", in_list_view=1, description="For in / between, separate values with commas. For is, use set or not set. Relative dates use a number of days."),
    ], child=True)
    schema("WA Template Variable", [
        field("component", "Select", options="Body\nHeader\nButton", default="Body", reqd=1, in_list_view=1, columns=2),
        field("position", "Int", default=1, reqd=1, in_list_view=1, columns=1),
        field("source_type", "Select", options="Field\nFixed", default="Field", reqd=1, in_list_view=1, columns=2),
        field("field_name", depends_on="eval:doc.source_type == 'Field'", in_list_view=1, columns=2),
        field("fixed_value", "Small Text", depends_on="eval:doc.source_type == 'Fixed'", in_list_view=1, columns=3,
              description="Text to send to every recipient when Source Type is Fixed."),
        field("fallback_value", "Small Text"),
    ], child=True, editable=True)
    schema("WA Campaign", [
        field("title", reqd=1, in_list_view=1),
        field("status", "Select", options="Draft\nActive\nPaused\nCompleted", default="Draft", read_only=1, in_list_view=1),
        field("account_section", "Section Break", "1. Account and template"),
        field("channel_account", "Link", options="Chat Channel Account", reqd=1, in_list_view=1),
        field("template_picker", "HTML"),
        field("template_name", reqd=1), field("language_code", default="en", reqd=1),
        field("template_preview", "Small Text", read_only=1),
        field("variables", "Table", options="WA Template Variable"),
        field("header_media_url", description="HTTPS image, video or document URL for templates with media headers."),
        field("audience_section", "Section Break", "2. Audience"),
        field("reference_doctype", "Select", options="Patient\nCRM Lead\nLead", default="Patient", reqd=1, in_list_view=1),
        field("phone_field", reqd=1, description="Choose a phone field from the field selector."),
        field("match_mode", "Select", options="All\nAny", default="All", reqd=1),
        field("filters", "Table", options="WA Campaign Filter"),
        field("sort_field", default="creation", reqd=1),
        field("sort_direction", "Select", options="asc\ndesc", default="desc", reqd=1),
        field("excluded_records", "Small Text", description="Optional document IDs to exclude, one per line."),
        field("consent_section", "Section Break", "Recipient preferences"),
        field("consent_confirmed", "Check", "I confirm this audience has permission to receive these WhatsApp messages", default=0),
        field("repeat_policy", "Select", options="Every matching run\nOnly new recipients", default="Every matching run", reqd=1),
        field("minimum_gap_days", "Int", default=0),
        field("preview_section", "Section Break", "Audience preview"), field("audience_preview", "HTML"),
        field("schedule_section", "Section Break", "3. Sending schedule"),
        field("delivery_mode", "Select", options="Now\nOnce\nWeekly", default="Now", reqd=1),
        field("timezone", default="Asia/Kolkata", reqd=1),
        field("start_date", "Date", depends_on="eval:doc.delivery_mode != 'Now'"),
        field("end_date", "Date", depends_on="eval:doc.delivery_mode == 'Weekly'"),
        field("send_time", "Time", default="10:00:00", depends_on="eval:doc.delivery_mode != 'Now'"),
        *[field(d.lower(), "Check", d, depends_on="eval:doc.delivery_mode == 'Weekly'") for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")],
        field("next_run_at", "Datetime", "Next run (IST)", read_only=1),
        field("schedule_preview", "HTML"),
        field("last_error", "Small Text", read_only=1),
    ])
    schema("WA Campaign Run", [
        field("campaign", "Link", options="WA Campaign", reqd=1, in_list_view=1),
        field("status", "Select", options="Queued\nPreparing\nSending\nCompleted\nFailed\nMissed\nCancelled", default="Queued", in_list_view=1),
        field("is_test", "Check", "Test send", default=0, in_list_view=1),
        field("run_key", unique=1), field("scheduled_for", "Datetime", "Scheduled for (IST)", in_list_view=1),
        field("snapshot_json", "Code", options="JSON", permlevel=1),
        field("started_at", "Datetime", "Started at (IST)"), field("finished_at", "Datetime", "Finished at (IST)"),
        field("matched", "Int"), field("eligible", "Int"), field("accepted", "Int"),
        field("delivered", "Int"), field("read_count", "Int", "Read"), field("failed", "Int"), field("skipped", "Int"), field("unknown", "Int"),
        field("error", "Small Text"),
    ], readonly=True)
    schema("WA Campaign Recipient", [
        field("campaign", "Link", options="WA Campaign", in_list_view=1),
        field("run", "Link", options="WA Campaign Run", in_list_view=1),
        field("is_test", "Check", "Test send", default=0),
        field("reference_doctype", "Link", options="DocType"), field("reference_name", "Dynamic Link", options="reference_doctype", in_list_view=1),
        field("recipient_key", unique=1), field("phone", permlevel=1, search_index=1), field("masked_phone", in_list_view=1),
        field("status", "Select", options="Pending\nSending\nRetry\nUnknown\nAccepted\nSent\nDelivered\nRead\nFailed\nSkipped", default="Pending", in_list_view=1),
        field("payload_json", "Code", options="JSON", permlevel=1), field("preview", "Small Text", permlevel=1),
        field("provider_message_id", search_index=1), field("chat_message", "Link", options="Chat Message"),
        field("attempts", "Int"), field("next_attempt_at", "Datetime", "Next attempt (IST)"),
        field("dispatch_started_at", "Datetime", "Dispatch started (IST)"), field("accepted_at", "Datetime", "Accepted at (IST)"),
        field("last_error", "Small Text"), field("history_error", "Small Text"),
    ], readonly=True)
    schema("WA Automation Settings", [
        field("sending_enabled", "Check", default=1),
        field("max_recipients", "Int", default=10000, description="Runs exceeding this many matching records fail before any messages are sent."),
        field("messages_per_minute", "Int", default=60, description="Shared per-account limit across WA Automation campaigns. Leave headroom for other apps using the same account."),
        field("max_attempts", "Int", default=3),
        field("schedule_grace_minutes", "Int", default=15, description="Older occurrences are marked missed rather than sent late."),
        field("scheduler_last_seen", "Datetime", "Scheduler last checked (IST)", read_only=1),
    ], single=True)
    workspace = ROOT / "workspace" / "wa_automation"
    workspace.mkdir(parents=True, exist_ok=True)
    links = [("WA Campaign", "Campaigns"), ("WA Campaign Run", "Run History"), ("WA Campaign Recipient", "Recipients"), ("WA Automation Settings", "Settings")]
    content = [{"id":"wa-title","type":"header","data":{"text":"<span class=\"h4\">WA Automation</span>","col":12}}]
    content += [{"id":f"wa-{i}","type":"shortcut","data":{"shortcut_name":label,"col":3}} for i, (_,label) in enumerate(links)]
    doc = dict(doctype="Workspace", name="WA Automation", label="WA Automation", title="WA Automation", module="WA Automation", public=1, is_hidden=0, icon="message", content=json.dumps(content), shortcuts=[dict(label=label,type="DocType",link_to=dt,doc_view="List" if dt != "WA Automation Settings" else "") for dt,label in links], links=[], roles=[{"role":"WA Automation User"},{"role":"WA Automation Manager"},{"role":"System Manager"}])
    (workspace / "wa_automation.json").write_text(json.dumps(doc, indent=2) + "\n")


if __name__ == "__main__":
    main()
