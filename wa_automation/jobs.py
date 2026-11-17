"""Durable runs. Claim before external I/O; ambiguous outcomes are never auto-retried."""
from contextlib import contextmanager
from datetime import timedelta
import json
import time

import frappe
from frappe.utils import cint

from wa_automation import audience
from wa_automation.core import utcnow, as_utc, stable_key, schedule_next, SUCCESS
from wa_automation.permissions import require_sender

OPEN_RUNS = ["Queued", "Preparing", "Sending"]


@contextmanager
def as_owner(user):
    previous = frappe.session.user
    try:
        if not frappe.db.get_value("User", user, "enabled"):
            frappe.throw("The campaign owner is disabled.")
        frappe.set_user(user)
        require_sender()
        yield
    finally:
        frappe.set_user(previous)


def create_run(doc, instant, request_key):
    with frappe.cache().lock(f"wa_automation:campaign:{doc.name}", timeout=30, blocking_timeout=5):
        doc.reload()
        if not cint(frappe.db.get_single_value("WA Automation Settings", "sending_enabled")):
            frappe.throw("WA Automation sending is disabled in Settings.")
        key = stable_key(doc.name, request_key)
        existing = frappe.db.get_value("WA Campaign Run", {"run_key": key}, "name")
        if existing:
            return existing
        if request_key.startswith("scheduled:") and (doc.status != "Active" or not doc.next_run_at or as_utc(doc.next_run_at) != as_utc(instant)):
            return None  # A pause/end/edit won the race with the scheduler.
        if frappe.db.exists("WA Campaign Run", {"campaign": doc.name, "is_test": 0, "status": ["in", OPEN_RUNS]}):
            frappe.throw("This campaign already has an unfinished run. Resume it before starting another.")
        snapshot = doc.as_dict()
        run = frappe.get_doc({"doctype": "WA Campaign Run", "campaign": doc.name, "owner": doc.owner,
            "run_key": key, "scheduled_for": as_utc(instant).replace(tzinfo=None), "status": "Queued",
            "snapshot_json": json.dumps(snapshot, default=str)}).insert(ignore_permissions=True)
        if doc.delivery_mode == "Now":
            frappe.db.set_value("WA Campaign", doc.name, "status", "Active")
        frappe.db.commit()
    enqueue_run(run.name)
    return run.name


def enqueue_run(name):
    from frappe.utils.background_jobs import get_queues_timeout
    queue = frappe.conf.get("wa_automation_queue") or "long"
    # Workers fork with the parent's cached queue list. A newly configured
    # custom queue can be present in site config but absent from that old list.
    if queue not in get_queues_timeout() and queue in (frappe.conf.get("workers") or {}):
        get_queues_timeout.cache_clear()
    frappe.enqueue("wa_automation.jobs.process_run", queue=queue, timeout=900,
        job_id=f"wa_automation:{name}", deduplicate=True, run_name=name, enqueue_after_commit=True)


def tick():
    """Cron entrypoint: no patient messages are sent in the scheduler process."""
    now = utcnow()
    frappe.db.set_single_value("WA Automation Settings", "scheduler_last_seen", now.replace(tzinfo=None))
    settings = frappe.get_single("WA Automation Settings")
    recover_stale_sends()
    if not cint(settings.sending_enabled):
        frappe.db.commit()
        return
    # Frappe comparisons can coalesce NULL datetimes to an empty value. A Once
    # campaign has no next occurrence while its queued run is still processing.
    campaigns = frappe.get_all("WA Campaign", filters=[
        ["status", "=", "Active"], ["next_run_at", "is", "set"],
        ["next_run_at", "<=", now.replace(tzinfo=None)],
    ], pluck="name")
    for name in campaigns:
        try:
            doc = frappe.get_doc("WA Campaign", name)
            # Pause/end or another scheduler may have changed it after the query.
            if doc.status != "Active" or not doc.next_run_at:
                continue
            due = as_utc(doc.next_run_at)
            missed = (now - due) > timedelta(minutes=max(1, cint(settings.schedule_grace_minutes or 15)))
            overlapping = frappe.db.exists("WA Campaign Run", {"campaign": name, "is_test": 0, "status": ["in", OPEN_RUNS]})
            if missed or overlapping:
                key = stable_key(name, "scheduled:" + due.isoformat())
                if not frappe.db.exists("WA Campaign Run", {"run_key": key}):
                    frappe.get_doc({"doctype": "WA Campaign Run", "campaign": name, "owner": doc.owner,
                        "run_key": key, "scheduled_for": due.replace(tzinfo=None), "status": "Missed",
                        "error": "An earlier run was still in progress." if overlapping else "Scheduled time was outside the catch-up grace period."}).insert(ignore_permissions=True)
            else:
                with as_owner(doc.owner):
                    doc.check_permission("read")
                    if not create_run(doc, due, "scheduled:" + due.isoformat()):
                        continue
            future = schedule_next(doc.as_dict(), now)
            frappe.db.set_value("WA Campaign", {"name": name, "status": "Active", "next_run_at": due.replace(tzinfo=None)}, "next_run_at", future.replace(tzinfo=None) if future else None)
            if not future and missed and doc.delivery_mode == "Once":
                frappe.db.set_value("WA Campaign", name, "status", "Completed")
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.db.set_value("WA Campaign", name, {"status": "Paused", "last_error": "Scheduling failed. Check the WA Automation error log and resume after correcting the configuration."})
            frappe.log_error(frappe.get_traceback(), "WA Automation scheduling")
            frappe.db.commit()
    # Also recovers queued work after a Redis / worker restart.
    for row in frappe.get_all("WA Campaign Run", filters={"status": ["in", OPEN_RUNS]}, fields=["name", "campaign", "is_test"]):
        if row.is_test or frappe.db.get_value("WA Campaign", row.campaign, "status") == "Active":
            enqueue_run(row.name)
    from wa_automation.delivery import reconcile
    reconcile()
    frappe.db.commit()


def recover_stale_sends():
    stale = (utcnow() - timedelta(minutes=5)).replace(tzinfo=None)
    rows = frappe.get_all("WA Campaign Recipient", filters={"status": "Sending", "dispatch_started_at": ["<", stale]}, fields=["name", "run"], limit_page_length=500)
    for row in rows:
        frappe.db.set_value("WA Campaign Recipient", {"name": row.name, "status": "Sending", "dispatch_started_at": ["<", stale]},
            {"status": "Unknown", "last_error": "Dispatch worker stopped; reconcile before any manual resend."})
    for name in {row.run for row in rows}:
        update_totals(name)


def prepare_run(run):
    config = json.loads(run.snapshot_json)
    rows = audience.build_audience(config, anchor=run.scheduled_for)
    # One transaction: a failed snapshot never leaves a partially sendable audience.
    for row in rows:
        row.update(doctype="WA Campaign Recipient", owner=run.owner, campaign=run.campaign,
            run=run.name, reference_doctype=config["reference_doctype"],
            recipient_key=stable_key(run.name, row["reference_name"]))
        frappe.get_doc(row).insert(ignore_permissions=True)
    frappe.db.set_value("WA Campaign Run", run.name, {"status": "Sending", "started_at": utcnow().replace(tzinfo=None), "matched": len(rows), "eligible": sum(r["status"] == "Pending" for r in rows)})
    frappe.db.commit()


def can_continue(campaign, is_test=False):
    return (is_test or frappe.db.get_value("WA Campaign", campaign, "status") == "Active") and cint(frappe.db.get_single_value("WA Automation Settings", "sending_enabled", cache=False))


def process_run(run_name):
    lock = frappe.cache().lock(f"wa_automation:run:{run_name}", timeout=600, blocking_timeout=0)
    if not lock.acquire(blocking=False):
        return
    try:
        run = frappe.get_doc("WA Campaign Run", run_name)
        if run.status not in OPEN_RUNS or not can_continue(run.campaign, run.is_test):
            return
        with as_owner(run.owner):
            frappe.get_doc("WA Campaign", run.campaign).check_permission("read")
            config = json.loads(run.snapshot_json)
            audience.validate_config(config, ready=True)
            if run.status in {"Queued", "Preparing"}:
                prepare_run(run)
            stale = (utcnow() - timedelta(minutes=5)).replace(tzinfo=None)
            for name in frappe.get_all("WA Campaign Recipient", filters={"run": run.name, "status": "Sending", "dispatch_started_at": ["<", stale]}, pluck="name"):
                frappe.db.set_value("WA Campaign Recipient", name, {"status": "Unknown", "last_error": "Worker stopped after dispatch claim; reconcile before any manual resend."})
            frappe.db.commit()
            # Refresh template validity once per worker batch, before any sends.
            audience.selected_template(config, audience.approved_templates(config, refresh=True))
            deadline = time.monotonic() + 45
            names = frappe.get_all("WA Campaign Recipient", filters={"run": run.name, "status": ["in", ["Pending", "Retry"]]},
                or_filters=[["next_attempt_at", "is", "not set"], ["next_attempt_at", "<=", utcnow().replace(tzinfo=None)]],
                fields=["name", "next_attempt_at"], order_by="creation asc", limit_page_length=100)
            for row in names:
                if time.monotonic() > deadline or not can_continue(run.campaign, run.is_test):
                    break
                if row.next_attempt_at and as_utc(row.next_attempt_at) > utcnow():
                    continue
                recipient = frappe.get_doc("WA Campaign Recipient", row.name)
                if not dispatch(config, recipient):
                    break  # Per-account budget exhausted; next tick resumes.
            update_totals(run.name)
    except Exception:
        frappe.db.rollback()
        # A failed batch pauses pending work; accepted or unknown rows remain intact.
        run = frappe.get_doc("WA Campaign Run", run_name)
        if run.status == "Queued" or run.is_test:
            frappe.db.set_value("WA Campaign Run", run_name, {"status": "Failed", "error": "Audience preparation failed; see WA Automation error log."})
        else:
            frappe.db.set_value("WA Campaign Run", run_name, "error", "Sending paused after an error; see WA Automation error log.")
        if not run.is_test:
            frappe.db.set_value("WA Campaign", run.campaign, {"status": "Paused", "last_error": "Run needs attention. Check run history and error log."})
        frappe.log_error(frappe.get_traceback(), "WA Automation run")
        frappe.db.commit()
    finally:
        lock.release()


def dispatch(config, recipient):
    try:
        account = audience.recheck_recipient(config, recipient)
    except Exception:
        frappe.db.set_value("WA Campaign Recipient", recipient.name, {"status": "Skipped", "last_error": "Recipient preferences, number or permissions changed after preparation."})
        frappe.db.commit()
        return True
    if not account.get_password("interakt_api_key", raise_exception=False):
        frappe.db.set_value("WA Campaign Recipient", recipient.name, {"status": "Failed", "last_error": "Interakt API key is missing on the selected account."})
        if not recipient.is_test:
            frappe.db.set_value("WA Campaign", recipient.campaign, {"status": "Paused", "last_error": "Configure the Interakt API key on the selected account before resuming."})
        frappe.db.commit()
        return True
    with frappe.cache().lock(f"wa_automation:account:{account.name}", timeout=60, blocking_timeout=5):
        if not can_continue(recipient.campaign, recipient.is_test):
            return False
        minute = int(time.time() // 60)
        key = f"wa_automation:budget:{account.name}:{minute}"
        used = cint(frappe.cache().get_value(key, expires=True))
        limit = max(1, cint(frappe.db.get_single_value("WA Automation Settings", "messages_per_minute") or 60))
        if used >= limit:
            return False
        frappe.cache().set_value(key, used + 1, expires_in_sec=120)
        # Persist claim before making an external request. A crash leaves Unknown, not Pending.
        attempts = cint(recipient.attempts) + 1
        frappe.db.set_value("WA Campaign Recipient", recipient.name, {"status": "Sending", "attempts": attempts, "dispatch_started_at": utcnow().replace(tzinfo=None), "last_error": ""})
        frappe.db.commit()
        payload = json.loads(recipient.payload_json)
        payload["callbackData"] = "wa_automation:" + recipient.name
        from wa_chat_hub.outbound import send_interakt_message
        from wa_chat_hub.delivery_outcomes import PatientTemplateNotSentError, PatientTemplateOutcomeUnknownError
        try:
            result = send_interakt_message(account, {"channel_account": account.name, "channel_type": "Interakt", "payload": payload})
            if not result.get("sent") or not result.get("provider_message_id"):
                raise PatientTemplateOutcomeUnknownError("Provider acceptance could not be confirmed.")
        except PatientTemplateNotSentError as exc:
            status_code = getattr(exc, "status_code", None)
            maximum = max(1, cint(frappe.db.get_single_value("WA Automation Settings", "max_attempts") or 3))
            # Only 429 is known not to have accepted the message. 408/5xx can be ambiguous.
            state = "Retry" if status_code == 429 and attempts < maximum else "Failed"
            if status_code == 408 or (status_code and status_code >= 500):
                state = "Unknown"
            frappe.db.set_value("WA Campaign Recipient", recipient.name, {"status": state,
                "next_attempt_at": (utcnow() + timedelta(seconds=min(3600, 60 * 2 ** attempts))).replace(tzinfo=None) if state == "Retry" else None,
                "last_error": f"Provider rejected request (HTTP {status_code or 'validation'})."})
            if status_code in {401, 403}:
                frappe.db.set_value("WA Campaign", recipient.campaign, {"status": "Paused", "last_error": "Interakt rejected account credentials or account access."})
        except Exception:
            frappe.db.set_value("WA Campaign Recipient", recipient.name, {"status": "Unknown", "last_error": "Provider outcome is uncertain. Automatic retry is disabled to prevent duplicate messages."})
        else:
            # Preserve a webhook that arrived before the HTTP response.
            current = frappe.db.get_value("WA Campaign Recipient", recipient.name, "status", for_update=True)
            state = current if current in {"Sent", "Delivered", "Read", "Failed"} else "Accepted"
            frappe.db.set_value("WA Campaign Recipient", recipient.name, {"status": state, "provider_message_id": result["provider_message_id"], "accepted_at": utcnow().replace(tzinfo=None), "last_error": ""})
            frappe.db.commit()
            append_history(config, recipient, result)
        frappe.db.commit()
    return True


def append_history(config, recipient, result):
    try:
        from wa_chat_hub.services import append_message
        saved = append_message({"channel_account": config["channel_account"], "phone_number": recipient.phone,
            "direction": "Outbound", "sender_type": "System", "content_type": "Template",
            "body": recipient.preview or "Template: " + config["template_name"], "delivery_status": "Pending",
            "channel_message_id": result["provider_message_id"], "provider_message_id": result["provider_message_id"],
            "provider_event_id": "wa_automation:" + recipient.name, "dedupe_key": "wa_automation:" + recipient.name,
            "provider_name": "Interakt", "raw_transport_payload": {"wa_automation_recipient": recipient.name}})
        frappe.db.set_value("WA Campaign Recipient", recipient.name, "chat_message", saved.get("message"))
    except Exception:
        # Never change the send outcome or resend because chat-history insertion failed.
        frappe.db.rollback()
        frappe.db.set_value("WA Campaign Recipient", recipient.name, "history_error", "Accepted by Interakt; chat history could not be updated. Delivery reconciliation remains enabled.")


def update_totals(name):
    run = frappe.db.get_value("WA Campaign Run", name,
        ["campaign", "status", "is_test", "finished_at"], as_dict=True)
    counts = {row.status: row.total for row in frappe.get_all("WA Campaign Recipient", filters={"run": name}, fields=["status", "count(name) as total"], group_by="status")}
    values = {"accepted": sum(counts.get(s, 0) for s in SUCCESS), "delivered": sum(counts.get(s, 0) for s in ("Delivered", "Read")), "read_count": counts.get("Read", 0), "failed": counts.get("Failed", 0), "skipped": counts.get("Skipped", 0), "unknown": counts.get("Unknown", 0)}
    if run.status != "Cancelled" and not any(counts.get(s, 0) for s in ("Pending", "Sending", "Retry")):
        values.update(status="Completed", finished_at=run.finished_at or utcnow().replace(tzinfo=None))
        # Late delivery/read receipts on an old run must not complete a campaign
        # that has since been activated again. Only a newly completed run can
        # finish the campaign, and only when no other run is still waiting.
        if not run.is_test and run.status != "Completed":
            state, next_run = frappe.db.get_value("WA Campaign", run.campaign, ["status", "next_run_at"])
            other_open = frappe.db.exists("WA Campaign Run", {"campaign": run.campaign,
                "name": ["!=", name], "is_test": 0, "status": ["in", OPEN_RUNS]})
            if state == "Active" and not next_run and not other_open:
                frappe.db.set_value("WA Campaign", run.campaign, "status", "Completed")
    frappe.db.set_value("WA Campaign Run", name, values)
    frappe.db.commit()
