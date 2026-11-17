"""Explicit run controls; preserve dispatch history and uncertain send outcomes."""
import json

import frappe
from frappe.utils import cint

from wa_automation import audience, jobs
from wa_automation.core import utcnow, schedule_next
from wa_automation.permissions import require_app, require_sender

CANCEL_REASON = "Cancelled before dispatch by a user."


def remove_waiting_job(name):
    from frappe.utils.background_jobs import get_job
    from rq.job import JobStatus
    job = get_job(f"wa_automation:{name}")
    if job and job.get_status() == JobStatus.QUEUED:
        job.cancel()


@frappe.whitelist(methods=["POST"])
def control(name, action):
    require_app()
    require_sender()
    run = frappe.get_doc("WA Campaign Run", name)
    run.check_permission("read")
    campaign = frappe.get_doc("WA Campaign", run.campaign)
    campaign.check_permission("write")
    if action not in {"cancel", "retry"}:
        frappe.throw("Choose Cancel or Retry.")
    # Never reset recipient claims while a worker is making provider requests.
    lock = frappe.cache().lock(f"wa_automation:run:{name}", timeout=60, blocking_timeout=0)
    if not lock.acquire(blocking=False):
        frappe.throw("This run is currently processing a batch. Pause the campaign, then retry this action after the batch finishes.")
    try:
        with frappe.cache().lock(f"wa_automation:campaign:{run.campaign}", timeout=60, blocking_timeout=5):
            run.reload()
            campaign.reload()
            if action == "cancel":
                if run.status == "Cancelled":
                    return {"status": "Cancelled"}
                if run.status not in jobs.OPEN_RUNS + ["Failed"]:
                    frappe.throw("Only unfinished or failed runs can be cancelled.")
                frappe.db.set_value("WA Campaign Recipient", {"run":name, "status":["in", ["Pending", "Retry"]]},
                    {"status":"Skipped", "last_error":CANCEL_REASON, "next_attempt_at":None})
                # An interrupted dispatch might already have reached Interakt.
                frappe.db.set_value("WA Campaign Recipient", {"run":name, "status":"Sending"},
                    {"status":"Unknown", "last_error":"Run cancelled after dispatch started; check delivery before any resend."})
                frappe.db.set_value("WA Campaign Run", name, {"status":"Cancelled",
                    "finished_at":utcnow().replace(tzinfo=None), "error":f"Cancelled by {frappe.session.user}."})
                if not run.is_test:
                    frappe.db.set_value("WA Campaign", campaign.name,
                        {"status":"Paused", "next_run_at":None, "last_error":""})
                jobs.update_totals(name)
                remove_waiting_job(name)
                return {"status":"Cancelled"}

            if run.status not in jobs.OPEN_RUNS + ["Failed", "Cancelled"]:
                frappe.throw("Only unfinished, failed or cancelled runs can be retried. Create a new schedule for a missed run.")
            if not cint(frappe.db.get_single_value("WA Automation Settings", "sending_enabled", cache=False)):
                frappe.throw("WA Automation sending is disabled in Settings.")
            if not run.snapshot_json:
                frappe.throw("This run has no saved configuration. Create a new schedule from the campaign.")
            if not run.is_test and frappe.db.exists("WA Campaign Run", {"campaign":campaign.name,
                    "name":["!=",name], "is_test":0, "status":["in",jobs.OPEN_RUNS]}):
                frappe.throw("Another run is unfinished. Cancel or finish it before retrying this one.")
            with jobs.as_owner(run.owner):
                frappe.get_doc("WA Campaign", campaign.name).check_permission("read")
                audience.validate_config(json.loads(run.snapshot_json), ready=True)
            # A queue entry marked started may still be alive even if its run lock
            # expired. Do not launch a second worker in that situation.
            from frappe.utils.background_jobs import get_job
            from rq.job import JobStatus
            job = get_job(f"wa_automation:{name}")
            if job and job.get_status() == JobStatus.STARTED:
                frappe.throw("The queue worker is still running this job. Pause the campaign and wait for the worker to finish before retrying.")
            recipients = frappe.get_all("WA Campaign Recipient", filters={"run":name},
                fields=["name","status","last_error","accepted_at","provider_message_id"])
            retryable = [r for r in recipients if not r.accepted_at and not r.provider_message_id
                and (r.status in {"Pending","Retry","Failed"}
                     or (r.status == "Skipped" and r.last_error == CANCEL_REASON))]
            if recipients and not retryable:
                frappe.throw("No safe pending recipients to retry. Accepted, sent, delivered, read and unknown outcomes are not resent.")
            for recipient in retryable:
                frappe.db.set_value("WA Campaign Recipient", recipient.name,
                    {"status":"Pending", "next_attempt_at":None, "last_error":""})
            frappe.db.set_value("WA Campaign Run", name, {"status":"Sending" if recipients else "Queued",
                "finished_at":None, "error":""})
            if not run.is_test:
                next_run = schedule_next(campaign.as_dict()) if campaign.delivery_mode == "Weekly" else None
                frappe.db.set_value("WA Campaign", campaign.name, {"status":"Active", "last_error":"",
                    "next_run_at":next_run.replace(tzinfo=None) if next_run else None})
            frappe.db.commit()
            remove_waiting_job(name)
            jobs.enqueue_run(name)
            frappe.db.commit()
            return {"status": "Sending" if recipients else "Queued"}
    finally:
        lock.release()
